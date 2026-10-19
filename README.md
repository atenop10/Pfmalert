# Pocket FM Episode Alert Bot

A Telegram bot that watches Pocket FM series you choose and messages you when new episodes appear.

> **Status:** untested against the live Pocket FM site. Pocket FM has no public API, so the bot reads public show pages. Run `/debug` on a real show link first and tune `COUNT_KEYS` in `pocketfm_bot.py` if needed. Check Pocket FM's terms of service before running it, keep polling modest, and don't redistribute their content. This project is unofficial and not affiliated with Pocket FM.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Set the token and run:
   ```bash
   export BOT_TOKEN="your-token"
   python pocketfm_bot.py
   ```

## Commands

| Command | What it does |
|---|---|
| `/follow <series link>` | Start tracking a series |
| `/unfollow <series link>` | Stop tracking it |
| `/list` | Show what you follow |
| `/debug <series link>` | Show what the scraper detected |

The bot checks every 20 minutes (`POLL_MINUTES`) and alerts only for series you follow.

## Notes

- Episodes may be counted when added, not when unlocked for free.
- If the page loads data via JavaScript, switch `fetch()` to Playwright.
- Never commit your bot token. `.env` and `*.db` are git-ignored.

## License

MIT
