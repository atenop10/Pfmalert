"""
Pocket FM Episode Alert Bot for Telegram.

Users:   /follow /unfollow /list /help
Admins:  /stats /users /userinfo /shows /broadcast /ban /unban /removeshow
         /forcecheck /debug /backup /adminhelp

Configuration (environment variables or a .env file next to this script):
    BOT_TOKEN          required, from @BotFather
    ADMIN_IDS          comma-separated Telegram user IDs allowed to use admin commands
    ALLOWED_CHAT_IDS   optional, comma-separated. If set, only these chats (and admins) can use the bot
    POLL_MINUTES       optional, default 20
    MAX_FOLLOWS        optional, max shows per chat, default 20
    NOTIFY_ON_START    optional, "1" (default) or "0": DM admins when the bot starts

NOTE: Pocket FM has no public API. The bot reads the public show page, so the
episode-count detection may need tuning. Use /debug <link> (admin) to see what
was found and adjust COUNT_KEYS below. Respect Pocket FM's terms of service.
"""

import asyncio
import functools
import json
import logging
import os
import re
import sqlite3
import time
from contextlib import closing
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from telegram import BotCommand, BotCommandScopeChat, Update
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter
from telegram.ext import Application, CommandHandler, ContextTypes

try:  # optional: load a .env file next to this script
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass


# ---------- config ----------

def _ids(name: str) -> set:
    raw = os.environ.get(name, "")
    return {int(x) for x in raw.replace(" ", "").split(",") if x.lstrip("-").isdigit()}


def _int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except ValueError:
        return default


BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_IDS = _ids("ADMIN_IDS")
ALLOWED_CHAT_IDS = _ids("ALLOWED_CHAT_IDS")
POLL_MINUTES = _int("POLL_MINUTES", 20)
MAX_FOLLOWS = _int("MAX_FOLLOWS", 20)
NOTIFY_ON_START = os.environ.get("NOTIFY_ON_START", "1").strip() != "0"

FETCH_DELAY_SECONDS = 2        # pause between show fetches while polling
FAIL_ALERT_THRESHOLD = 10      # tell admins after this many failed checks in a row
FOLLOW_COOLDOWN_SECONDS = 3    # per-chat cooldown for /follow
DROP_CONFIRM_CHECKS = 3        # a lower episode count must repeat this many polls before we trust it
MAX_SEND_LEN = 4000            # Telegram limit is 4096

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "pocketfm_bot.db")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("pocketfm-bot")

START_TIME = time.time()
POLL_LOCK = asyncio.Lock()
_follow_last_used: dict = {}

_session = requests.Session()
_session.headers.update(HEADERS)
_session.mount("https://", HTTPAdapter(pool_connections=2, pool_maxsize=2))
_session.mount("http://", HTTPAdapter(pool_connections=2, pool_maxsize=2))


# ---------- storage ----------

def _add_column(con, table: str, column: str, decl: str):
    cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
    if column not in cols:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def db() -> sqlite3.Connection:
    """Open the DB, create tables, and migrate older schemas."""
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=15000")
    con.execute(
        "CREATE TABLE IF NOT EXISTS follows ("
        "chat_id INTEGER, url TEXT, PRIMARY KEY (chat_id, url))"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS shows ("
        "url TEXT PRIMARY KEY, title TEXT, last_count INTEGER)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS users ("
        "chat_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, "
        "first_seen INTEGER, last_seen INTEGER, banned INTEGER DEFAULT 0)"
    )
    con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    # migrations for databases created by older versions
    _add_column(con, "shows", "last_checked", "INTEGER")
    _add_column(con, "shows", "fail_count", "INTEGER DEFAULT 0")
    _add_column(con, "shows", "last_error", "TEXT")
    _add_column(con, "shows", "drop_hits", "INTEGER DEFAULT 0")
    _add_column(con, "users", "blocked", "INTEGER DEFAULT 0")
    _add_column(con, "users", "commands_used", "INTEGER DEFAULT 0")
    con.commit()
    return con


def meta_get(con, key: str, default=0):
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def meta_set(con, key: str, value):
    con.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def meta_inc(con, key: str, n: int = 1):
    meta_set(con, key, int(meta_get(con, key, 0)) + n)


def now() -> int:
    return int(time.time())


# ---------- scraping ----------

def valid(url: str) -> bool:
    return bool(re.match(r"^https?://([a-z0-9-]+\.)?pocketfm\.com(/|$)", url, re.I))


def normalize_url(url: str) -> str:
    """Strip query, fragment and trailing slash so the same show always matches."""
    p = urlsplit(url.strip())
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), "", ""))


class FetchError(Exception):
    pass


def fetch(url: str) -> str:
    """Blocking fetch with retries. Always call via asyncio.to_thread() from async code."""
    last_err = "unknown error"
    for attempt in range(3):
        try:
            r = _session.get(url, timeout=20, allow_redirects=True)
        except requests.RequestException as e:
            last_err = f"network error: {type(e).__name__}"
            time.sleep(2 * (attempt + 1))
            continue
        if not valid(r.url):
            raise FetchError(f"Redirected outside pocketfm.com: {r.url}")
        if r.status_code in (403, 429, 503):
            last_err = f"HTTP {r.status_code} (blocked or rate limited)"
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code >= 500:
            last_err = f"HTTP {r.status_code}"
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 404:
            raise FetchError("HTTP 404 (show page not found)")
        if r.status_code >= 400:
            raise FetchError(f"HTTP {r.status_code}")
        return r.text
    raise FetchError(last_err)


def _walk(obj, path=""):
    """Yield (path, value) for every leaf in nested JSON."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        yield (path + "[]", len(obj))  # list length is often the episode count
        for i, v in enumerate(obj[:3]):
            yield from _walk(v, f"{path}[{i}]")
    else:
        yield path, obj


def extract_json_blobs(html: str):
    """Pull structured data embedded in the page (most reliable source)."""
    soup = BeautifulSoup(html, "html.parser")
    blobs = []
    nxt = soup.find("script", id="__NEXT_DATA__")
    if nxt and (nxt.string or nxt.get_text()):
        try:
            blobs.append(("__NEXT_DATA__", json.loads(nxt.string or nxt.get_text())))
        except json.JSONDecodeError:
            pass
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            blobs.append(("ld+json", json.loads(s.string or s.get_text() or "")))
        except json.JSONDecodeError:
            pass
    return soup, blobs


COUNT_KEYS = re.compile(
    r"^(total_?episodes?|episodes?_?count|num_?episodes?|story_?count|numberOfEpisodes)$",
    re.I,
)
TITLE_SUFFIX = re.compile(r"\s*[|\-–]\s*pocket\s*fm.*$", re.I)


def clean_title(raw: str) -> str:
    return TITLE_SUFFIX.sub("", raw or "").strip() or "Unknown show"


def find_episode_info(html: str):
    """
    Returns (title, episode_count) or (title, None) if the count can't be found.

    Order of trust: ld+json fields, then __NEXT_DATA__ fields (shallowest first,
    so the main show wins over recommended shows), then visible text.
    Tune COUNT_KEYS after running /debug.
    """
    soup, blobs = extract_json_blobs(html)
    raw_title = soup.title.string.strip() if soup.title and soup.title.string else ""
    title = clean_title(raw_title)

    candidates = []  # (source_rank, depth, count)
    for name, blob in blobs:
        rank = 0 if name == "ld+json" else 1
        for path, val in _walk(blob):
            last_key = re.sub(r"\[\d*\]$", "", path.split(".")[-1])
            if COUNT_KEYS.match(last_key) and isinstance(val, (int, float, str)) and not isinstance(val, bool):
                try:
                    n = int(float(str(val).replace(",", "")))
                except (TypeError, ValueError):
                    continue
                if n >= 0:
                    candidates.append((rank, path.count(".") + path.count("["), n))
    if candidates:
        candidates.sort(key=lambda c: (c[0], c[1]))
        return title, candidates[0][2]

    m = re.search(r"(\d[\d,]*)\s+episodes?", soup.get_text(" ", strip=True), re.I)
    if m:
        return title, int(m.group(1).replace(",", ""))
    return title, None


# ---------- helpers ----------

def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    parts = [f"{d}d"] if d else []
    if h or d:
        parts.append(f"{h}h")
    parts.append(f"{m}m")
    if not d and not h:
        parts.append(f"{s}s")
    return " ".join(parts)


def fmt_ago(ts) -> str:
    if not ts:
        return "never"
    return fmt_duration(max(0, now() - int(ts))) + " ago"


def is_admin(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id in ADMIN_IDS)


async def reply(update: Update, text: str):
    await update.message.reply_text(text[:MAX_SEND_LEN], disable_web_page_preview=True)


def registered(fn):
    """Track the user, enforce bans and the optional allow-list."""
    @functools.wraps(fn)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not update.message or not update.effective_chat:
            return
        chat_id = update.effective_chat.id
        user = update.effective_user
        admin = is_admin(update)

        with closing(db()) as con, con:
            con.execute(
                "INSERT INTO users (chat_id, username, first_name, first_seen, last_seen, commands_used) "
                "VALUES (?,?,?,?,?,1) ON CONFLICT(chat_id) DO UPDATE SET "
                "username=excluded.username, first_name=excluded.first_name, "
                "last_seen=excluded.last_seen, blocked=0, "
                "commands_used=COALESCE(commands_used,0)+1",
                (chat_id, user.username if user else None, user.first_name if user else None, now(), now()),
            )
            banned = con.execute("SELECT banned FROM users WHERE chat_id=?", (chat_id,)).fetchone()[0]

        if banned and not admin:
            await reply(update, "You are not allowed to use this bot.")
            return
        if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS and not admin:
            await reply(update, "Sorry, this bot is private.")
            return
        return await fn(update, ctx)
    return wrapper


def admin_only(fn):
    @functools.wraps(fn)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update):
            return  # stay silent so regular users don't learn admin commands exist
        return await fn(update, ctx)
    return registered(wrapper)


async def safe_send(bot, chat_id: int, text: str) -> str:
    """Send a message. Returns 'ok', 'blocked' (user left/blocked) or 'fail'."""
    for _ in range(3):
        try:
            await bot.send_message(chat_id, text[:MAX_SEND_LEN])
            return "ok"
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except Forbidden:
            return "blocked"
        except BadRequest as e:
            if "chat not found" in str(e).lower():
                return "blocked"
            log.warning("send to %s failed: %s", chat_id, e)
            return "fail"
        except NetworkError as e:
            log.warning("network error sending to %s: %s", chat_id, e)
            await asyncio.sleep(2)
        except Exception as e:
            log.warning("send to %s failed: %s", chat_id, e)
            return "fail"
    return "fail"


async def notify_admins(bot, text: str):
    for aid in ADMIN_IDS:
        await safe_send(bot, aid, text)


def drop_chat(chat_id: int):
    """User blocked the bot: remove their follows (and orphaned shows) and mark them blocked."""
    with closing(db()) as con, con:
        con.execute("DELETE FROM follows WHERE chat_id=?", (chat_id,))
        con.execute("UPDATE users SET blocked=1 WHERE chat_id=?", (chat_id,))
        con.execute("DELETE FROM shows WHERE url NOT IN (SELECT url FROM follows)")


# ---------- user commands ----------

HELP_TEXT = (
    "I message you when a followed Pocket FM show gets new episodes.\n\n"
    "/follow <show link> - start tracking\n"
    "/unfollow <link or number from /list> - stop tracking\n"
    "/list - what you follow\n"
    "/help - this message"
)


@registered
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await reply(update, HELP_TEXT)


@registered
async def follow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args or not valid(ctx.args[0]):
        await reply(update, "Usage: /follow https://pocketfm.com/show/...")
        return
    chat_id = update.effective_chat.id

    last = _follow_last_used.get(chat_id, 0)
    if time.time() - last < FOLLOW_COOLDOWN_SECONDS:
        await reply(update, "Slow down a little and try again.")
        return
    _follow_last_used[chat_id] = time.time()

    url = normalize_url(ctx.args[0])
    with closing(db()) as con:
        n = con.execute("SELECT COUNT(*) FROM follows WHERE chat_id=?", (chat_id,)).fetchone()[0]
        already = con.execute(
            "SELECT 1 FROM follows WHERE chat_id=? AND url=?", (chat_id, url)
        ).fetchone()
        known = con.execute("SELECT title, last_count FROM shows WHERE url=?", (url,)).fetchone()
    if already:
        await reply(update, "You're already following that show.")
        return
    if n >= MAX_FOLLOWS and not is_admin(update):
        await reply(update, f"You can follow at most {MAX_FOLLOWS} shows.")
        return

    if known and known[1] is not None:
        # Already tracked for someone else: no need to hit Pocket FM again.
        title, count = known
    else:
        try:
            html = await asyncio.to_thread(fetch, url)
            title, count = find_episode_info(html)
        except Exception as e:
            await reply(update, f"Couldn't load that page: {str(e)[:200]}")
            return
        if count is None:
            await reply(
                update,
                "I loaded the page but couldn't find an episode count, so I can't track it reliably.",
            )
            return

    with closing(db()) as con, con:
        con.execute("INSERT OR IGNORE INTO follows (chat_id, url) VALUES (?, ?)", (chat_id, url))
        con.execute(
            "INSERT OR IGNORE INTO shows (url, title, last_count, last_checked, fail_count) "
            "VALUES (?,?,?,?,0)",
            (url, title, count, now()),
        )
        row = con.execute("SELECT title, last_count FROM shows WHERE url=?", (url,)).fetchone()
    await reply(update, f"Following: {row[0]}\nCurrent episodes: {row[1]}")


def _user_follows(chat_id: int):
    with closing(db()) as con:
        return con.execute(
            "SELECT s.title, s.last_count, s.url FROM follows f JOIN shows s ON s.url=f.url "
            "WHERE f.chat_id=? ORDER BY s.title COLLATE NOCASE",
            (chat_id,),
        ).fetchall()


@registered
async def unfollow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await reply(update, "Usage: /unfollow <link or number from /list>")
        return
    chat_id = update.effective_chat.id
    arg = ctx.args[0]
    if arg.isdigit():
        rows = _user_follows(chat_id)
        idx = int(arg)
        if not 1 <= idx <= len(rows):
            await reply(update, "No show with that number. Check /list.")
            return
        url = rows[idx - 1][2]
    else:
        url = normalize_url(arg)
    with closing(db()) as con, con:
        removed = con.execute(
            "DELETE FROM follows WHERE chat_id=? AND url=?", (chat_id, url)
        ).rowcount
        con.execute("DELETE FROM shows WHERE url NOT IN (SELECT url FROM follows)")
    await reply(update, "Done." if removed else "You weren't following that link.")


@registered
async def list_follows(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = _user_follows(update.effective_chat.id)
    if not rows:
        await reply(update, "You're not following anything yet.")
        return
    text = "\n\n".join(f"{i}. {t} ({c} eps)\n{u}" for i, (t, c, u) in enumerate(rows, 1))
    await reply(update, text + "\n\nStop tracking: /unfollow <number>")


# ---------- admin commands ----------

ADMIN_HELP = (
    "Admin commands:\n"
    "/stats - bot statistics\n"
    "/users - recent users\n"
    "/userinfo <chat_id> - details and follows of one user\n"
    "/shows - tracked shows and health\n"
    "/broadcast <text> - message all users\n"
    "/ban <chat_id> - block a user\n"
    "/unban <chat_id> - unblock a user\n"
    "/removeshow <link> - delete a show and its follows\n"
    "/forcecheck - poll all shows now\n"
    "/debug <link> - show what the scraper detects\n"
    "/backup - send the database file"
)


@admin_only
async def adminhelp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await reply(update, ADMIN_HELP)


@admin_only
async def stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with closing(db()) as con:
        q = lambda sql, *a: con.execute(sql, a).fetchone()[0]  # noqa: E731
        users = q("SELECT COUNT(*) FROM users")
        banned = q("SELECT COUNT(*) FROM users WHERE banned=1")
        blocked = q("SELECT COUNT(*) FROM users WHERE blocked=1")
        active = q("SELECT COUNT(*) FROM users WHERE last_seen>?", now() - 86400)
        new_week = q("SELECT COUNT(*) FROM users WHERE first_seen>?", now() - 7 * 86400)
        follows = q("SELECT COUNT(*) FROM follows")
        shows = q("SELECT COUNT(*) FROM shows")
        failing = q("SELECT COUNT(*) FROM shows WHERE fail_count>=?", FAIL_ALERT_THRESHOLD)
        alerts = int(meta_get(con, "alerts_sent", 0))
        polls = int(meta_get(con, "polls_run", 0))
        last_poll = int(meta_get(con, "last_poll_at", 0))
        last_dur = float(meta_get(con, "last_poll_seconds", 0))
        last_alerts = int(meta_get(con, "last_poll_alerts", 0))
        last_failed = int(meta_get(con, "last_poll_failed", 0))
    size_kb = os.path.getsize(DB_PATH) // 1024 if os.path.exists(DB_PATH) else 0
    await reply(
        update,
        "Bot stats\n"
        f"Uptime: {fmt_duration(time.time() - START_TIME)}\n"
        f"Poll interval: {POLL_MINUTES} min\n\n"
        f"Users: {users} (active 24h: {active}, new 7d: {new_week})\n"
        f"Banned: {banned}, blocked the bot: {blocked}\n"
        f"Follows: {follows}\n"
        f"Shows tracked: {shows} (failing: {failing})\n\n"
        f"Alerts sent (total): {alerts}\n"
        f"Polls run: {polls}\n"
        f"Last poll: {fmt_ago(last_poll)}, took {last_dur:.1f}s, "
        f"{last_alerts} alerts, {last_failed} failed\n"
        f"Database: {size_kb} KB",
    )


@admin_only
async def users_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with closing(db()) as con:
        rows = con.execute(
            "SELECT u.chat_id, u.username, u.first_name, u.banned, u.blocked, u.last_seen, "
            "(SELECT COUNT(*) FROM follows f WHERE f.chat_id=u.chat_id) "
            "FROM users u ORDER BY u.last_seen DESC LIMIT 30"
        ).fetchall()
    if not rows:
        await reply(update, "No users yet.")
        return
    lines = []
    for cid, uname, fname, banned, blocked, seen, nf in rows:
        name = f"@{uname}" if uname else (fname or "?")
        flag = (" [BANNED]" if banned else "") + (" [BLOCKED BOT]" if blocked else "")
        lines.append(f"{cid} {name} - {nf} follows, seen {fmt_ago(seen)}{flag}")
    await reply(update, "Latest 30 users:\n" + "\n".join(lines))


@admin_only
async def userinfo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args or not ctx.args[0].lstrip("-").isdigit():
        await reply(update, "Usage: /userinfo <chat_id>")
        return
    cid = int(ctx.args[0])
    with closing(db()) as con:
        u = con.execute(
            "SELECT username, first_name, first_seen, last_seen, banned, blocked, commands_used "
            "FROM users WHERE chat_id=?",
            (cid,),
        ).fetchone()
    if not u:
        await reply(update, "No such user.")
        return
    uname, fname, first, last, banned, blocked, used = u
    rows = _user_follows(cid)
    shows = "\n".join(f"- {t} ({c} eps)" for t, c, _ in rows) or "- none"
    await reply(
        update,
        f"User {cid}\nName: {fname or '?'} {('@' + uname) if uname else ''}\n"
        f"First seen: {fmt_ago(first)}\nLast seen: {fmt_ago(last)}\n"
        f"Commands used: {used or 0}\nBanned: {bool(banned)}, blocked bot: {bool(blocked)}\n\n"
        f"Follows ({len(rows)}):\n{shows}",
    )


@admin_only
async def shows_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with closing(db()) as con:
        rows = con.execute(
            "SELECT s.title, s.url, s.last_count, s.fail_count, s.last_checked, s.last_error, "
            "(SELECT COUNT(*) FROM follows f WHERE f.url=s.url) AS n "
            "FROM shows s ORDER BY n DESC, s.fail_count DESC LIMIT 25"
        ).fetchall()
    if not rows:
        await reply(update, "No shows tracked.")
        return
    lines = []
    for title, url, count, fails, checked, err, n in rows:
        line = f"{(title or '?')[:50]}\n{url}\n{count} eps, {n} followers, checked {fmt_ago(checked)}"
        if fails:
            line += f"\nFAILING x{fails}: {err or 'unknown'}"
        lines.append(line)
    await reply(update, "\n\n".join(lines))


async def _do_broadcast(bot, admin_chat: int, text: str):
    with closing(db()) as con:
        ids = [r[0] for r in con.execute("SELECT chat_id FROM users WHERE banned=0 AND COALESCE(blocked,0)=0")]
    ok = blocked = failed = 0
    for cid in ids:
        status = await safe_send(bot, cid, text)
        if status == "ok":
            ok += 1
        elif status == "blocked":
            blocked += 1
            drop_chat(cid)
        else:
            failed += 1
        await asyncio.sleep(0.05)  # stay well under Telegram's rate limits
    await safe_send(bot, admin_chat, f"Broadcast done. Sent: {ok}, blocked: {blocked}, failed: {failed}")


@admin_only
async def broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.partition(" ")[2].strip()
    if not text:
        await reply(update, "Usage: /broadcast <message>")
        return
    with closing(db()) as con:
        n = con.execute("SELECT COUNT(*) FROM users WHERE banned=0 AND COALESCE(blocked,0)=0").fetchone()[0]
    await reply(update, f"Sending to {n} users...")
    # Run in the background so the bot stays responsive during large broadcasts.
    ctx.application.create_task(_do_broadcast(ctx.bot, update.effective_chat.id, text))


async def _set_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE, value: int):
    verb = "ban" if value else "unban"
    if not ctx.args or not ctx.args[0].lstrip("-").isdigit():
        await reply(update, f"Usage: /{verb} <chat_id>")
        return
    cid = int(ctx.args[0])
    if cid in ADMIN_IDS:
        await reply(update, "You can't do that to an admin.")
        return
    with closing(db()) as con, con:
        cur = con.execute("UPDATE users SET banned=? WHERE chat_id=?", (value, cid))
    await reply(update, f"{verb.capitalize()}ned {cid}." if cur.rowcount else "No such user.")


@admin_only
async def ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _set_ban(update, ctx, 1)


@admin_only
async def unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _set_ban(update, ctx, 0)


@admin_only
async def removeshow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await reply(update, "Usage: /removeshow <link>")
        return
    url = normalize_url(ctx.args[0])
    with closing(db()) as con, con:
        f = con.execute("DELETE FROM follows WHERE url=?", (url,)).rowcount
        s = con.execute("DELETE FROM shows WHERE url=?", (url,)).rowcount
    await reply(update, f"Removed {s} show and {f} follows." if s else "Show not found.")


@admin_only
async def forcecheck(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if POLL_LOCK.locked():
        await reply(update, "A check is already running.")
        return
    await reply(update, "Checking all shows now...")
    r = await poll_once(ctx.bot)
    await reply(
        update,
        f"Done in {r['seconds']:.1f}s. Checked: {r['checked']}, failed: {r['failed']}, alerts sent: {r['alerts']}",
    )


@admin_only
async def debug(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args or not valid(ctx.args[0]):
        await reply(update, "Usage: /debug <pocketfm show link>")
        return
    try:
        html = await asyncio.to_thread(fetch, normalize_url(ctx.args[0]))
    except Exception as e:
        await reply(update, f"Fetch failed: {str(e)[:300]}")
        return
    _, blobs = extract_json_blobs(html)
    title, count = find_episode_info(html)
    hits = []
    for name, blob in blobs:
        for path, val in _walk(blob):
            if re.search(r"episode|count|total", path, re.I) and not isinstance(val, str):
                hits.append(f"{name}: {path} = {val}")
    msg = f"Title: {title}\nDetected count: {count}\nJSON blobs: {[n for n, _ in blobs]}\n"
    msg += "\n".join(hits[:25]) or "No episode-like fields found in embedded JSON."
    await reply(update, msg)


@admin_only
async def backup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tmp = os.path.join(BASE_DIR, f"backup-{now()}.db")
    try:
        with closing(sqlite3.connect(DB_PATH)) as src, closing(sqlite3.connect(tmp)) as dst:
            src.backup(dst)
        with open(tmp, "rb") as f:
            await update.message.reply_document(f, filename=f"pocketfm_bot-{time.strftime('%Y%m%d-%H%M%S')}.db")
    except Exception as e:
        await reply(update, f"Backup failed: {str(e)[:200]}")
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# ---------- polling ----------

async def poll_once(bot) -> dict:
    """Check every tracked show once. Returns a summary dict."""
    async with POLL_LOCK:
        t0 = time.time()
        checked = failed = alerts = 0
        with closing(db()) as con:
            shows = con.execute(
                "SELECT url, title, last_count, COALESCE(fail_count,0), COALESCE(drop_hits,0) FROM shows"
            ).fetchall()

        for url, title, last, fails, drop_hits in shows:
            error, count = None, None
            try:
                html = await asyncio.to_thread(fetch, url)
                _, count = find_episode_info(html)
                if count is None:
                    error = "episode count not found on page"
            except Exception as e:
                error = str(e)[:200]
            checked += 1

            if error:
                failed += 1
                fails += 1
                with closing(db()) as con, con:
                    con.execute(
                        "UPDATE shows SET fail_count=?, last_error=?, last_checked=? WHERE url=?",
                        (fails, error, now(), url),
                    )
                log.warning("check failed for %s (%dx): %s", url, fails, error)
                if fails == FAIL_ALERT_THRESHOLD:
                    await notify_admins(bot, f"Show failing {fails} checks in a row:\n{title}\n{url}\n{error}")
            else:
                if fails >= FAIL_ALERT_THRESHOLD:
                    await notify_admins(bot, f"Show recovered:\n{title}\n{url}")

                new_count, new_drop = count, 0
                if last is not None and count < last:
                    # Pages sometimes render partial data. Only trust a lower count if it repeats.
                    new_drop = drop_hits + 1
                    if new_drop < DROP_CONFIRM_CHECKS:
                        new_count = last
                        log.info("lower count for %s (%s < %s), waiting for confirmation", url, count, last)
                    else:
                        new_drop = 0
                with closing(db()) as con, con:
                    con.execute(
                        "UPDATE shows SET fail_count=0, last_error=NULL, last_checked=?, "
                        "last_count=?, drop_hits=? WHERE url=?",
                        (now(), new_count, new_drop, url),
                    )

                if last is not None and count > last:
                    new = count - last
                    with closing(db()) as con:
                        chats = [
                            r[0]
                            for r in con.execute(
                                "SELECT f.chat_id FROM follows f LEFT JOIN users u ON u.chat_id=f.chat_id "
                                "WHERE f.url=? AND COALESCE(u.banned,0)=0 AND COALESCE(u.blocked,0)=0",
                                (url,),
                            )
                        ]
                    for chat in chats:
                        status = await safe_send(
                            bot, chat, f"New episode alert: {title}\n+{new} (now {count} episodes)\n{url}"
                        )
                        if status == "ok":
                            alerts += 1
                        elif status == "blocked":
                            drop_chat(chat)
                        await asyncio.sleep(0.05)
            await asyncio.sleep(FETCH_DELAY_SECONDS)

        seconds = time.time() - t0
        with closing(db()) as con, con:
            meta_inc(con, "polls_run")
            meta_inc(con, "alerts_sent", alerts)
            meta_set(con, "last_poll_at", now())
            meta_set(con, "last_poll_seconds", round(seconds, 2))
            meta_set(con, "last_poll_alerts", alerts)
            meta_set(con, "last_poll_failed", failed)
        return {"checked": checked, "failed": failed, "alerts": alerts, "seconds": seconds}


async def poll_job(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        await poll_once(ctx.bot)
    except Exception:
        log.exception("poll failed")


# ---------- startup / errors ----------

async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    if isinstance(ctx.error, NetworkError):
        log.warning("Network error: %s", ctx.error)  # transient, don't spam tracebacks or users
        return
    log.error("Unhandled error", exc_info=ctx.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("Something went wrong. Please try again later.")
    except Exception:
        pass


async def post_init(app: Application):
    user_cmds = [
        BotCommand("follow", "Follow a Pocket FM show"),
        BotCommand("unfollow", "Stop following a show"),
        BotCommand("list", "Show what you follow"),
        BotCommand("help", "How to use this bot"),
    ]
    admin_cmds = user_cmds + [
        BotCommand("stats", "Bot statistics"),
        BotCommand("users", "Recent users"),
        BotCommand("userinfo", "Details of one user"),
        BotCommand("shows", "Tracked shows"),
        BotCommand("broadcast", "Message all users"),
        BotCommand("ban", "Ban a user"),
        BotCommand("unban", "Unban a user"),
        BotCommand("removeshow", "Remove a show"),
        BotCommand("forcecheck", "Poll all shows now"),
        BotCommand("debug", "Debug scraper on a link"),
        BotCommand("backup", "Send database backup"),
        BotCommand("adminhelp", "Admin command list"),
    ]
    try:
        await app.bot.set_my_commands(user_cmds)
        for aid in ADMIN_IDS:
            try:
                await app.bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(aid))
            except Exception as e:
                log.warning("couldn't set admin commands for %s: %s", aid, e)
    except Exception as e:
        log.warning("set_my_commands failed: %s", e)
    if NOTIFY_ON_START:
        await notify_admins(app.bot, f"Bot started. Polling every {POLL_MINUTES} min.")


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set. Put it in .env or export it.")
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS is empty: admin commands are disabled.")

    db().close()  # create/migrate the database at startup

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .concurrent_updates(True)  # a slow command (/forcecheck, /follow) must not block everyone else
        .build()
    )
    if app.job_queue is None:
        raise SystemExit('Job queue missing. Run: pip install "python-telegram-bot[job-queue]"')

    for name, fn in [
        ("start", start), ("help", start), ("follow", follow), ("unfollow", unfollow),
        ("list", list_follows), ("adminhelp", adminhelp), ("stats", stats), ("users", users_cmd),
        ("userinfo", userinfo), ("shows", shows_cmd), ("broadcast", broadcast), ("ban", ban),
        ("unban", unban), ("removeshow", removeshow), ("forcecheck", forcecheck),
        ("debug", debug), ("backup", backup),
    ]:
        app.add_handler(CommandHandler(name, fn))
    app.add_error_handler(on_error)

    app.job_queue.run_repeating(
        poll_job,
        interval=POLL_MINUTES * 60,
        first=30,
        job_kwargs={"max_instances": 1, "coalesce": True},
    )
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
