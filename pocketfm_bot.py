"""
Pocket FM episode alert bot for Telegram.

Setup:
    pip install "python-telegram-bot[job-queue]>=20" requests beautifulsoup4
    export BOT_TOKEN="123456:ABC..."   # from @BotFather
    python pocketfm_bot.py

Commands:
    /follow <pocketfm show url>   start tracking a show
    /unfollow <pocketfm show url> stop tracking
    /list                         show what you follow
    /debug <pocketfm show url>    print what the scraper found (use this first!)

IMPORTANT: I could not test this against the live Pocket FM site. Pocket FM has
no public API, so this reads the public show page. Run /debug on a real show
URL first and, if needed, adjust find_episode_info() to match the page's data.
Check Pocket FM's terms of service and keep POLL_MINUTES modest.
"""
import json
import logging
import os
import re
import sqlite3

import requests
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.environ["BOT_TOKEN"]
POLL_MINUTES = 20
DB_PATH = "pocketfm_bot.db"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; EpisodeAlertBot/1.0)"}

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pocketfm-bot")


# ---------- storage ----------
def db():
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "CREATE TABLE IF NOT EXISTS follows ("
        "chat_id INTEGER, url TEXT, PRIMARY KEY (chat_id, url))"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS shows ("
        "url TEXT PRIMARY KEY, title TEXT, last_count INTEGER)"
    )
    return con


# ---------- scraping ----------
def fetch(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.text


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
    r"(total_?episodes?|episodes?_?count|num_?episodes?|story_?count|numberOfEpisodes)",
    re.I,
)


def find_episode_info(html: str):
    """
    Returns (title, episode_count) or (title, None) if the count can't be found.
    Order of trust: explicit count field in embedded JSON -> visible text.
    Tune COUNT_KEYS after running /debug on a real show page.
    """
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


# ---------- bot commands ----------
def valid(url: str) -> bool:
    return bool(re.match(r"^https?://(www\.)?pocketfm\.com/", url))


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send /follow <Pocket FM show link> and I'll message you when a new episode appears.\n"
        "Other commands: /unfollow <link>, /list, /debug <link>"
    )


async def follow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args or not valid(ctx.args[0]):
        await update.message.reply_text("Usage: /follow https://pocketfm.com/show/...")
        return
    url = ctx.args[0].split("?")[0]
    try:
        title, count = find_episode_info(fetch(url))
    except Exception as e:
        await update.message.reply_text(f"Couldn't load that page: {e}")
        return
    if count is None:
        await update.message.reply_text(
            "I loaded the page but couldn't find an episode count, so I can't track it reliably. "
            "Try /debug with this link."
        )
        return
    con = db()
    con.execute("INSERT OR IGNORE INTO follows VALUES (?, ?)", (update.effective_chat.id, url))
    con.execute("INSERT OR IGNORE INTO shows VALUES (?, ?, ?)", (url, title, count))
    con.commit()
    await update.message.reply_text(f"Following: {title}\nCurrent episodes: {count}")


async def unfollow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: /unfollow <link>")
        return
    con = db()
    con.execute(
        "DELETE FROM follows WHERE chat_id=? AND url=?",
        (update.effective_chat.id, ctx.args[0].split("?")[0]),
    )
    con.commit()
    await update.message.reply_text("Done.")


async def list_follows(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = db().execute(
        "SELECT s.title, s.last_count, s.url FROM follows f JOIN shows s ON s.url=f.url "
        "WHERE f.chat_id=?",
        (update.effective_chat.id,),
    ).fetchall()
    if not rows:
        await update.message.reply_text("You're not following anything yet.")
        return
    await update.message.reply_text("\n\n".join(f"{t} ({c} eps)\n{u}" for t, c, u in rows))


async def debug(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args or not valid(ctx.args[0]):
        await update.message.reply_text("Usage: /debug <pocketfm show link>")
        return
    html = fetch(ctx.args[0])
    _, blobs = extract_json_blobs(html)
    title, count = find_episode_info(html)
    hits = []
    for name, blob in blobs:
        for path, val in _walk(blob):
            if re.search(r"episode|count|total", path, re.I) and not isinstance(val, str):
                hits.append(f"{name}: {path} = {val}")
    msg = f"Title: {title}\nDetected count: {count}\nJSON blobs: {[n for n, _ in blobs]}\n"
    msg += "\n".join(hits[:25]) or "No episode-like fields found in embedded JSON."
    await update.message.reply_text(msg[:4000])


# ---------- polling ----------
async def poll(ctx: ContextTypes.DEFAULT_TYPE):
    con = db()
    for url, title, last in con.execute("SELECT url, title, last_count FROM shows").fetchall():
        try:
            _, count = find_episode_info(fetch(url))
        except Exception as e:
            log.warning("fetch failed for %s: %s", url, e)
            continue
        if count is None or count <= last:
            continue
        new = count - last
        con.execute("UPDATE shows SET last_count=? WHERE url=?", (count, url))
        con.commit()
        chats = [r[0] for r in con.execute("SELECT chat_id FROM follows WHERE url=?", (url,))]
        for chat in chats:
            await ctx.bot.send_message(
                chat, f"New episode alert: {title}\n+{new} (now {count} episodes)\n{url}"
            )


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("follow", follow))
    app.add_handler(CommandHandler("unfollow", unfollow))
    app.add_handler(CommandHandler("list", list_follows))
    app.add_handler(CommandHandler("debug", debug))
    app.job_queue.run_repeating(poll, interval=POLL_MINUTES * 60, first=30)
    app.run_polling()


if __name__ == "__main__":
    main()
