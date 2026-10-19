# Pocket FM Alert Bot

Telegram bot that messages you when a Pocket FM show you follow gets new episodes.

**Users:** `/follow <link>`, `/unfollow <link or number>`, `/list`, `/help`
**Admins:** `/stats`, `/users`, `/userinfo`, `/shows`, `/broadcast`, `/ban`, `/unban`, `/removeshow`, `/forcecheck`, `/debug`, `/backup`, `/adminhelp`

> Pocket FM has no public API. The bot reads the public show page and compares the episode count.
> If a show can't be tracked, run `/debug <link>` as admin to see what the scraper finds, then adjust `COUNT_KEYS` in `pocketfm_bot.py`. Respect Pocket FM's terms of service.

## Quick VPS setup (one command)

```bash
git clone https://github.com/YOUR_USERNAME/pocketfm-alert-bot.git
cd pocketfm-alert-bot
sudo bash setup.sh
```
It installs everything, asks for your bot token and admin ID, starts the bot as a service and schedules daily backups.
Update later with `sudo bash /home/botuser/pocketfm-alert-bot/update.sh`.
(Private repo? Use a clone URL with a deploy key or token.)

## Manual VPS setup (Ubuntu/Debian)

```bash
# 1. As root: install tools and create a user
apt update && apt install -y python3 python3-venv python3-pip git
adduser --disabled-password --gecos "" botuser

# 2. As botuser: get the code
su - botuser
git clone https://github.com/YOUR_USERNAME/pocketfm-alert-bot.git
cd pocketfm-alert-bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. Configure
cp .env.example .env
nano .env            # set BOT_TOKEN and ADMIN_IDS
chmod 600 .env
exit                 # back to root

# 4. Run as a service (auto-start on boot, auto-restart on crash)
cp /home/botuser/pocketfm-alert-bot/pocketfm-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now pocketfm-bot
systemctl status pocketfm-bot
journalctl -u pocketfm-bot -f      # live logs
```

### Update after pushing new code to GitHub
```bash
su - botuser -c "cd pocketfm-alert-bot && git pull && .venv/bin/pip install -r requirements.txt"
systemctl restart pocketfm-bot
```

### Daily backups
```bash
chmod +x /home/botuser/pocketfm-alert-bot/backup.sh
su - botuser -c '(crontab -l 2>/dev/null; echo "0 3 * * * /home/botuser/pocketfm-alert-bot/backup.sh") | crontab -'
```
Or send yourself the DB any time with `/backup`.

### Moving an older database
Copy your old `pocketfm_bot.db` into the project folder before the first start. It is migrated automatically.

## Configuration (.env)

| Variable | Meaning | Default |
|---|---|---|
| `BOT_TOKEN` | Token from @BotFather | required |
| `ADMIN_IDS` | Comma-separated Telegram user IDs | none |
| `ALLOWED_CHAT_IDS` | If set, only these chats (+ admins) can use the bot | open |
| `POLL_MINUTES` | How often shows are checked | 20 |
| `MAX_FOLLOWS` | Max shows per user | 20 |
| `NOTIFY_ON_START` | DM admins on startup (1/0) | 1 |

## Only run one copy
Telegram allows one polling instance per token. If you see a `Conflict` error, stop the bot on your old host (Render, PC, etc.) first.
