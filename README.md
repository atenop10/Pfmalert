# Pocket FM Episode Alert Bot

A Telegram bot that watches Pocket FM series you choose and messages you when new episodes appear.

> **Status:** untested against the live Pocket FM site. Pocket FM has no public API, so the bot reads public show pages. Run `/debug` on a real show link first and tune `COUNT_KEYS` in `pocketfm_bot.py` if needed. Check Pocket FM's terms of service before running it, keep polling modest, and don't redistribute their content. This project is unofficial and not affiliated with Pocket FM.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Easiest: run `bash setup.sh` then `bash run.sh`. It asks for your bot token and your Telegram user ID.
   Manual alternative:
   ```bash
   export BOT_TOKEN="your-token"
   export ADMIN_IDS="your-telegram-id"   # get it from @userinfobot
   python pocketfm_bot.py
   ```

## Commands

**Public**

| Command | What it does |
|---|---|
| `/start`, `/help` | Intro and usage |
| `/follow <series link>` | Start tracking a series (5 per user by default) |
| `/unfollow <series link>` | Stop tracking it |
| `/list` | Show what you follow and your limit |
| `/requestmore [reason]` | Ask the admin for a higher limit |

**Admin** (only IDs in `ADMIN_IDS`)

| Command | What it does |
|---|---|
| `/adminhelp` | List admin commands |
| `/stats` | Users, active/new 24h, shows, follows, alerts sent, errors, uptime |
| `/users` | Recent users and follow counts |
| `/shows` | Most followed and failing shows |
| `/broadcast <text>` | Message all users |
| `/setlimit <id> <n\|default>` | Set a user's series limit (the user is notified) |
| `/forcesub on\|off` | Turn the channel requirement on or off |
| `/ban <id>`, `/unban <id>` | Block or unblock a user |
| `/debug <series link>` | Show what the scraper detects |

The bot checks every 5 minutes (`POLL_MINUTES`). Each series is fetched once per cycle no matter how many users follow it. Admins are warned if a series fails 10 checks in a row.

## Force-subscribe

Users must join the channel in `FORCE_SUB_CHANNEL` (default `@audios_fm`) before using the bot, and alerts pause for anyone who leaves. **Add the bot as an administrator of the channel** (any permissions are fine) or it can't check membership. If the check fails, users are let through and admins are warned. Set `FORCE_SUB_CHANNEL=` (empty) to disable.

## Notes

- Episodes may be counted when added, not when unlocked for free.
- If the page loads data via JavaScript, switch `fetch()` to Playwright.
- Never commit your bot token. `.env` and `*.db` are git-ignored.

## License

MIT
