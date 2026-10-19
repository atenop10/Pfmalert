"""
Pocket FM episode alert bot for Telegram (public + admin version).

Public commands:  /start /help /follow /unfollow /list
Admin commands:   /adminhelp /stats /users /shows /broadcast /ban /unban /debug

Config (environment variables, or .env loaded by run.sh):
    BOT_TOKEN   token from @BotFather                (required)
    ADMIN_IDS   your Telegram user ID(s), comma-sep  (required for admin cmds)

NOTE: Pocket FM has no public API, so this reads public show pages. It is
untested against the live site: run /debug on a real show link first and tune
COUNT_KEYS if the episode count isn't detected. Check Pocket FM's terms of
service before running this publicly.
"""
import asyncio
import functools
import json
import logging
import os
import re
import sqlite3
import time
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import Forbidden, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

BOT_TOKEN = os.environ["BOT_TOKEN"]
# Users must join this channel to use the bot. The bot must be an ADMIN of the channel.
# Set FORCE_SUB_CHANNEL="" to disable.
FORCE_SUB_CHANNEL = os.environ.get("FORCE_SUB_CHANNEL", "@audios_fm")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
POLL_MINUTES = 5
DEFAULT_FOLLOW_LIMIT = 5      # admins can raise this per user with /setlimit
FETCH_DELAY_SECONDS = 1.5      # pause between show fetches (be polite to the site)
FAILS_BEFORE_ADMIN_ALERT = 10
DB_PATH = "pocketfm_bot.db"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; EpisodeAlertBot/1.0)"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pocketfm-bot")
STARTED_AT = time.time()


# ---------------- storage ----------------
def db():
    con = sqlite3.connect(DB_PATH)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY, username TEXT, first_seen INTEGER,
            last_seen INTEGER, banned INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS follows (
            chat_id INTEGER, url TEXT, PRIMARY KEY (chat_id, url));
        CREATE TABLE IF NOT EXISTS shows (
            url TEXT PRIMARY KEY, title TEXT, last_count INTEGER,
            last_checked INTEGER DEFAULT 0, fails INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER);
        """
    )
    cols = [r[1] for r in con.execute("PRAGMA table_info(users)")]
    if "follow_limit" not in cols:  # migration for older databases
        con.execute("ALTER TABLE users ADD COLUMN follow_limit INTEGER")
    return con


def get_limit(con, chat_id):
    r = con.execute("SELECT follow_limit FROM users WHERE chat_id=?", (chat_id,)).fetchone()
    return r[0] if r and r[0] is not None else DEFAULT_FOLLOW_LIMIT


def bump(con, key, n=1):
    con.execute(
        "INSERT INTO meta VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = value + ?",
        (key, n, n),
    )


def meta(con, key):
    r = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r[0] if r else 0


# ---------------- scraping ----------------
def fetch(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    if not is_pocketfm(r.url):
        raise ValueError("redirected away from pocketfm.com")
    return r.text


def is_pocketfm(url: str) -> bool:
    try:
        u = urlparse(url)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return u.scheme in ("http", "https") and (host == "pocketfm.com" or host.endswith(".pocketfm.com"))


def normalize(url: str) -> str:
    u = urlparse(url.strip())
    return f"{u.scheme}://{u.netloc}{u.path}".rstrip("/")


def _walk(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        yield (path + "[]", len(obj))
        for i, v in enumerate(obj[:3]):
            yield from _walk(v, f"{path}[{i}]")
    else:
        yield path, obj


def extract_json_blobs(html: str):
    soup = BeautifulSoup(html, "html.parser")
    blobs = []
    nxt = soup.find("script", id="__NEXT_DATA__")
    if nxt and nxt.string:
        try:
            blobs.append(("__NEXT_DATA__", json.loads(nxt.string)))
        except json.JSONDecodeError:
            pass
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            blobs.append(("ld+json", json.loads(s.string or "")))
        except json.JSONDecodeError:
            pass
    return soup, blobs


COUNT_KEYS = re.compile(
    r"(total_?episodes?|episodes?_?count|num_?episodes?|story_?count|numberOfEpisodes)", re.I
)


def find_episode_info(html: str):
    """Return (title, episode_count or None). Tune COUNT_KEYS after /debug."""
    soup, blobs = extract_json_blobs(html)
    title = soup.title.string.strip() if soup.title and soup.title.string else "Unknown show"
    for _, blob in blobs:
        for path, val in _walk(blob):
            if COUNT_KEYS.search(path.split(".")[-1]) and isinstance(val, (int, float, str)):
                try:
                    return title, int(val)
                except (TypeError, ValueError):
                    continue
    m = re.search(r"(\d[\d,]*)\s+episodes?", soup.get_text(" ", strip=True), re.I)
    if m:
        return title, int(m.group(1).replace(",", ""))
    return title, None


async def get_info(url: str):
    html = await asyncio.to_thread(fetch, url)   # don't block the bot while fetching
    return find_episode_info(html)


# ---------------- force subscribe ----------------
_member_cache = {}        # user_id -> (timestamp, True); only positive results are cached
_sub_warned = False       # warn admins once if the channel check itself is broken


def forcesub_enabled(con=None):
    if not FORCE_SUB_CHANNEL:
        return False
    return meta(con or db(), "forcesub_off") == 0


def channel_url():
    return "https://t.me/" + FORCE_SUB_CHANNEL.lstrip("@")


async def is_member(bot, user_id, notify_ctx=None):
    """True if the user is in the channel. Fails open if the check itself breaks."""
    global _sub_warned
    if user_id in ADMIN_IDS:
        return True
    cached = _member_cache.get(user_id)
    if cached and time.time() - cached[0] < 600:
        return True
    try:
        m = await bot.get_chat_member(FORCE_SUB_CHANNEL, user_id)
    except TelegramError as e:
        log.error("force-sub check failed (is the bot an admin of %s?): %s", FORCE_SUB_CHANNEL, e)
        if notify_ctx and not _sub_warned:
            _sub_warned = True
            await notify_admins(
                notify_ctx,
                f"Force-sub check failed: {e}\nMake sure the bot is an ADMIN of {FORCE_SUB_CHANNEL}. "
                "Until fixed, users are allowed through.",
            )
        return True  # fail open so a config problem doesn't lock everyone out
    ok = m.status in ("member", "administrator", "creator") or (
        m.status == "restricted" and getattr(m, "is_member", False)
    )
    if ok:
        _member_cache[user_id] = (time.time(), True)
    return ok


def join_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Join channel", url=channel_url())],
        [InlineKeyboardButton("I've joined", callback_data="checksub")],
    ])


async def checksub_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if await is_member(ctx.bot, q.from_user.id, ctx):
        await q.answer("Thanks for joining!")
        await q.edit_message_text("Verified! Send /help to get started.")
    else:
        await q.answer("You haven't joined yet. Join the channel first, then tap again.", show_alert=True)


# ---------------- decorators ----------------
def track(fn):
    """Register/update the user and block banned users."""
    @functools.wraps(fn)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        u, chat = update.effective_user, update.effective_chat
        con = db()
        now = int(time.time())
        con.execute(
            "INSERT INTO users (chat_id, username, first_seen, last_seen) VALUES (?,?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET username=excluded.username, last_seen=excluded.last_seen",
            (chat.id, u.username if u else None, now, now),
        )
        banned = con.execute("SELECT banned FROM users WHERE chat_id=?", (chat.id,)).fetchone()[0]
        bump(con, "commands_total")
        con.commit()
        if banned:
            return
        if fn.__name__ not in ("start", "help_cmd") and forcesub_enabled(con):
            if not await is_member(ctx.bot, chat.id, ctx):
                await update.effective_message.reply_text(
                    f"To use this bot, please join our channel {FORCE_SUB_CHANNEL} first, "
                    "then tap \"I've joined\".",
                    reply_markup=join_keyboard(),
                )
                return
        return await fn(update, ctx)
    return wrapper


def admin_only(fn):
    @functools.wraps(fn)
    @track
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in ADMIN_IDS:
            return  # silently ignore non-admins
        return await fn(update, ctx)
    return wrapper


# ---------------- public commands ----------------
HELP = (
    "Pocket FM Episode Alert Bot\n\n"
    "I message you when new episodes appear for the series you follow.\n\n"
    "Commands:\n"
    "/follow <series link> - start tracking a series\n"
    "/unfollow <series link> - stop tracking it\n"
    "/list - see the series you follow and your limit\n"
    "/requestmore - ask the admin for a higher limit\n"
    "/help - show this message\n\n"
    "How to get a link: open the series in the Pocket FM app, tap Share, "
    "copy the link, and paste it after /follow.\n\n"
    f"Limits: {DEFAULT_FOLLOW_LIMIT} series per person by default (more with admin approval). "
    f"I check every {POLL_MINUTES} minutes.\n"
    "This bot is unofficial and not affiliated with Pocket FM."
)


@track
async def start(update, ctx):
    if forcesub_enabled() and not await is_member(ctx.bot, update.effective_user.id, ctx):
        await update.message.reply_text(
            f"Welcome! To use this bot, please join our channel {FORCE_SUB_CHANNEL} first, "
            "then tap \"I've joined\".",
            reply_markup=join_keyboard(),
        )
        return
    await update.message.reply_text("Welcome!\n\n" + HELP)


@track
async def help_cmd(update, ctx):
    await update.message.reply_text(HELP)


@track
async def follow(update, ctx):
    if not ctx.args or not is_pocketfm(ctx.args[0]):
        await update.message.reply_text("Usage: /follow https://pocketfm.com/show/...\nSend /help for more.")
        return
    url = normalize(ctx.args[0])
    chat_id = update.effective_chat.id
    con = db()
    if con.execute("SELECT 1 FROM follows WHERE chat_id=? AND url=?", (chat_id, url)).fetchone():
        await update.message.reply_text("You're already following that series.")
        return
    n = con.execute("SELECT COUNT(*) FROM follows WHERE chat_id=?", (chat_id,)).fetchone()[0]
    limit = get_limit(con, chat_id)
    if n >= limit:
        await update.message.reply_text(
            f"You've reached your limit of {limit} series. Use /unfollow to free a slot, "
            "or /requestmore to ask the admin for a higher limit."
        )
        return
    row = con.execute("SELECT title, last_count FROM shows WHERE url=?", (url,)).fetchone()
    if row:
        title, count = row
    else:
        try:
            title, count = await get_info(url)
        except Exception as e:
            log.warning("follow fetch failed %s: %s", url, e)
            await update.message.reply_text("I couldn't load that page. Check the link and try again.")
            return
        if count is None:
            await update.message.reply_text(
                "I opened the page but couldn't read an episode count, so I can't track this one."
            )
            return
        con.execute(
            "INSERT OR IGNORE INTO shows (url, title, last_count, last_checked) VALUES (?,?,?,?)",
            (url, title, count, int(time.time())),
        )
    con.execute("INSERT INTO follows VALUES (?, ?)", (chat_id, url))
    con.commit()
    await update.message.reply_text(f"Following: {title}\nCurrent episodes: {count}\nI'll alert you about new ones.")


@track
async def unfollow(update, ctx):
    if not ctx.args:
        await update.message.reply_text("Usage: /unfollow <series link>")
        return
    url = normalize(ctx.args[0])
    con = db()
    cur = con.execute("DELETE FROM follows WHERE chat_id=? AND url=?", (update.effective_chat.id, url))
    con.execute("DELETE FROM shows WHERE url NOT IN (SELECT url FROM follows)")  # drop unwatched shows
    con.commit()
    await update.message.reply_text("Unfollowed." if cur.rowcount else "You weren't following that link.")


@track
async def list_follows(update, ctx):
    rows = db().execute(
        "SELECT s.title, s.last_count, s.url FROM follows f JOIN shows s ON s.url=f.url WHERE f.chat_id=?",
        (update.effective_chat.id,),
    ).fetchall()
    if not rows:
        await update.message.reply_text("You're not following anything yet. Try /follow <series link>.")
        return
    limit = get_limit(db(), update.effective_chat.id)
    body = "\n\n".join(f"{t} ({c} eps)\n{u}" for t, c, u in rows)
    await update.message.reply_text(f"Following {len(rows)}/{limit}\n\n{body}")


@track
async def requestmore(update, ctx):
    if not ADMIN_IDS:
        await update.message.reply_text("Sorry, the admin can't be reached right now.")
        return
    u, chat = update.effective_user, update.effective_chat
    con = db()
    n = con.execute("SELECT COUNT(*) FROM follows WHERE chat_id=?", (chat.id,)).fetchone()[0]
    reason = " ".join(ctx.args).strip()[:300]
    await notify_admins(
        ctx,
        f"Limit request from {chat.id} (@{u.username or '-'})\n"
        f"Using {n}/{get_limit(con, chat.id)}\n"
        + (f"Reason: {reason}\n" if reason else "")
        + f"Approve with: /setlimit {chat.id} <new limit>",
    )
    await update.message.reply_text("Request sent to the admin. You'll be told if it's approved.")


# ---------------- admin commands ----------------
ADMIN_HELP = (
    "Admin commands:\n"
    "/stats - overview of users, shows, alerts, uptime\n"
    "/users - recent users and follow counts\n"
    "/shows - most followed shows and failing ones\n"
    "/broadcast <text> - message all users\n"
    "/setlimit <chat_id> <n> - set a user's series limit (/setlimit <chat_id> default resets it)\n"
    "/forcesub on|off - turn the channel requirement on or off\n"
    "/ban <chat_id> - block a user\n"
    "/unban <chat_id> - unblock a user\n"
    "/debug <series link> - show what the scraper detects"
)


@admin_only
async def adminhelp(update, ctx):
    await update.message.reply_text(ADMIN_HELP)


def fmt_uptime(sec):
    d, r = divmod(int(sec), 86400)
    h, r = divmod(r, 3600)
    return f"{d}d {h}h {r // 60}m"


@admin_only
async def stats(update, ctx):
    con = db()
    day = int(time.time()) - 86400
    q = lambda sql, *a: con.execute(sql, a).fetchone()[0]
    msg = (
        "Bot stats\n"
        f"Uptime: {fmt_uptime(time.time() - STARTED_AT)}\n"
        f"Users: {q('SELECT COUNT(*) FROM users')} "
        f"(active 24h: {q('SELECT COUNT(*) FROM users WHERE last_seen>?', day)}, "
        f"new 24h: {q('SELECT COUNT(*) FROM users WHERE first_seen>?', day)}, "
        f"banned: {q('SELECT COUNT(*) FROM users WHERE banned=1')})\n"
        f"Force-sub: {FORCE_SUB_CHANNEL or 'disabled'} ({'ON' if forcesub_enabled(con) else 'OFF'})\n"
        f"Shows tracked: {q('SELECT COUNT(*) FROM shows')}\n"
        f"Total follows: {q('SELECT COUNT(*) FROM follows')}\n"
        f"Alerts sent: {meta(con, 'alerts_sent')}\n"
        f"Poll cycles: {meta(con, 'polls')}\n"
        f"Fetch errors: {meta(con, 'fetch_errors')}\n"
        f"Commands handled: {meta(con, 'commands_total')}"
    )
    await update.message.reply_text(msg)


@admin_only
async def users(update, ctx):
    rows = db().execute(
        "SELECT u.chat_id, u.username, u.banned, COUNT(f.url), u.follow_limit FROM users u "
        "LEFT JOIN follows f ON f.chat_id=u.chat_id GROUP BY u.chat_id "
        "ORDER BY u.last_seen DESC LIMIT 25"
    ).fetchall()
    lines = [
        f"{cid} @{un or '-'} follows:{n}/{lim if lim is not None else DEFAULT_FOLLOW_LIMIT}"
        f"{' [BANNED]' if b else ''}"
        for cid, un, b, n, lim in rows
    ]
    await update.message.reply_text("Recent users:\n" + ("\n".join(lines) or "none"))


@admin_only
async def shows_cmd(update, ctx):
    con = db()
    top = con.execute(
        "SELECT s.title, COUNT(f.chat_id) c FROM shows s LEFT JOIN follows f ON f.url=s.url "
        "GROUP BY s.url ORDER BY c DESC LIMIT 10"
    ).fetchall()
    bad = con.execute("SELECT title, fails FROM shows WHERE fails>0 ORDER BY fails DESC LIMIT 10").fetchall()
    msg = "Top shows:\n" + ("\n".join(f"{c} - {t[:60]}" for t, c in top) or "none")
    if bad:
        msg += "\n\nFailing shows:\n" + "\n".join(f"{f} fails - {t[:60]}" for t, f in bad)
    await update.message.reply_text(msg)


@admin_only
async def broadcast(update, ctx):
    text = " ".join(ctx.args).strip()
    if not text:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    con = db()
    ids = [r[0] for r in con.execute("SELECT chat_id FROM users WHERE banned=0")]
    ok = fail = 0
    for cid in ids:
        try:
            await ctx.bot.send_message(cid, text)
            ok += 1
        except TelegramError:
            fail += 1
        await asyncio.sleep(0.05)  # stay under Telegram rate limits
    await update.message.reply_text(f"Broadcast done. Sent: {ok}, failed: {fail}")


async def _set_ban(update, ctx, value):
    if not ctx.args or not ctx.args[0].lstrip("-").isdigit():
        await update.message.reply_text("Usage: /ban <chat_id>  or  /unban <chat_id>")
        return
    con = db()
    cur = con.execute("UPDATE users SET banned=? WHERE chat_id=?", (value, int(ctx.args[0])))
    con.commit()
    await update.message.reply_text("Done." if cur.rowcount else "No such user.")


@admin_only
async def setlimit(update, ctx):
    if len(ctx.args) != 2 or not ctx.args[0].lstrip("-").isdigit() or not (
        ctx.args[1].isdigit() or ctx.args[1].lower() == "default"
    ):
        await update.message.reply_text("Usage: /setlimit <chat_id> <number|default>")
        return
    chat_id = int(ctx.args[0])
    new = None if ctx.args[1].lower() == "default" else min(int(ctx.args[1]), 1000)
    con = db()
    cur = con.execute("UPDATE users SET follow_limit=? WHERE chat_id=?", (new, chat_id))
    con.commit()
    if not cur.rowcount:
        await update.message.reply_text("No such user (they must have used the bot at least once).")
        return
    shown = new if new is not None else DEFAULT_FOLLOW_LIMIT
    await update.message.reply_text(f"Limit for {chat_id} set to {shown}.")
    try:
        await ctx.bot.send_message(chat_id, f"Your series limit is now {shown}.")
    except TelegramError:
        pass


@admin_only
async def forcesub_cmd(update, ctx):
    con = db()
    if not FORCE_SUB_CHANNEL:
        await update.message.reply_text("No channel configured (FORCE_SUB_CHANNEL is empty).")
        return
    arg = ctx.args[0].lower() if ctx.args else ""
    if arg in ("on", "off"):
        con.execute("INSERT INTO meta VALUES ('forcesub_off', ?) ON CONFLICT(key) DO UPDATE SET value=?",
                    (0 if arg == "on" else 1,) * 2)
        con.commit()
    state = "ON" if forcesub_enabled(con) else "OFF"
    await update.message.reply_text(f"Force-subscribe to {FORCE_SUB_CHANNEL} is {state}.\nUsage: /forcesub on|off")


@admin_only
async def ban(update, ctx):
    await _set_ban(update, ctx, 1)


@admin_only
async def unban(update, ctx):
    await _set_ban(update, ctx, 0)


@admin_only
async def debug(update, ctx):
    if not ctx.args or not is_pocketfm(ctx.args[0]):
        await update.message.reply_text("Usage: /debug <pocketfm series link>")
        return
    try:
        html = await asyncio.to_thread(fetch, ctx.args[0])
    except Exception as e:
        await update.message.reply_text(f"Fetch failed: {e}")
        return
    _, blobs = extract_json_blobs(html)
    title, count = find_episode_info(html)
    hits = [
        f"{name}: {path} = {val}"
        for name, blob in blobs
        for path, val in _walk(blob)
        if re.search(r"episode|count|total", path, re.I) and not isinstance(val, str)
    ]
    msg = f"Title: {title}\nDetected count: {count}\nJSON blobs: {[n for n, _ in blobs]}\n"
    msg += "\n".join(hits[:25]) or "No episode-like fields found."
    await update.message.reply_text(msg[:4000])


# ---------------- polling ----------------
async def notify_admins(ctx, text):
    for a in ADMIN_IDS:
        try:
            await ctx.bot.send_message(a, text)
        except TelegramError:
            pass


async def poll(ctx: ContextTypes.DEFAULT_TYPE):
    con = db()
    bump(con, "polls")
    con.commit()
    shows = con.execute("SELECT url, title, last_count, fails FROM shows").fetchall()
    for url, title, last, fails in shows:
        try:
            _, count = await get_info(url)
            if count is None:
                raise ValueError("episode count not found")
        except Exception as e:
            log.warning("poll failed %s: %s", url, e)
            fails += 1
            con.execute("UPDATE shows SET fails=?, last_checked=? WHERE url=?", (fails, int(time.time()), url))
            bump(con, "fetch_errors")
            con.commit()
            if fails == FAILS_BEFORE_ADMIN_ALERT:
                await notify_admins(ctx, f"Warning: {title} failed {fails} checks in a row.\n{url}\n{e}")
            await asyncio.sleep(FETCH_DELAY_SECONDS)
            continue

        con.execute("UPDATE shows SET fails=0, last_checked=? WHERE url=?", (int(time.time()), url))
        if count > last:
            con.execute("UPDATE shows SET last_count=? WHERE url=?", (count, url))
            con.commit()
            chats = [r[0] for r in con.execute(
                "SELECT f.chat_id FROM follows f JOIN users u ON u.chat_id=f.chat_id "
                "WHERE f.url=? AND u.banned=0", (url,))]
            for chat in chats:
                if forcesub_enabled(con) and not await is_member(ctx.bot, chat, ctx):
                    continue  # left the channel: alerts paused until they rejoin
                try:
                    await ctx.bot.send_message(
                        chat, f"New episode alert: {title}\n+{count - last} (now {count} episodes)\n{url}"
                    )
                    bump(con, "alerts_sent")
                except Forbidden:  # user blocked the bot: clean up their follows
                    con.execute("DELETE FROM follows WHERE chat_id=?", (chat,))
                except TelegramError as e:
                    log.warning("send failed to %s: %s", chat, e)
                await asyncio.sleep(0.05)
        con.commit()
        await asyncio.sleep(FETCH_DELAY_SECONDS)
    con.execute("DELETE FROM shows WHERE url NOT IN (SELECT url FROM follows)")
    con.commit()


def main():
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS not set: admin commands are disabled.")
    app = Application.builder().token(BOT_TOKEN).build()
    for name, fn in [
        ("start", start), ("help", help_cmd), ("follow", follow), ("unfollow", unfollow),
        ("list", list_follows), ("requestmore", requestmore), ("setlimit", setlimit), ("forcesub", forcesub_cmd),
        ("adminhelp", adminhelp), ("stats", stats), ("users", users),
        ("shows", shows_cmd), ("broadcast", broadcast), ("ban", ban), ("unban", unban),
        ("debug", debug),
    ]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(checksub_callback, pattern="^checksub$"))
    app.job_queue.run_repeating(poll, interval=POLL_MINUTES * 60, first=30, job_kwargs={"max_instances": 1})
    app.run_polling()


if __name__ == "__main__":
    main()
