#!/usr/bin/env bash
# One-command VPS installer for the Pocket FM alert bot (Ubuntu/Debian, run as root).
#
#   git clone https://github.com/YOUR_USERNAME/pocketfm-alert-bot.git
#   cd pocketfm-alert-bot && sudo bash setup.sh
#
# Optional env vars: BOT_USER (default botuser), REPO_URL (clone/pull instead of copying local files)
# Safe to re-run: it keeps your existing .env and database.
set -euo pipefail

BOT_USER="${BOT_USER:-botuser}"
APP_DIR="/home/${BOT_USER}/pocketfm-alert-bot"
SERVICE="pocketfm-bot"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mError: %s\033[0m\n' "$*" >&2; exit 1; }
ask() { # ask "prompt" VARNAME  (works even when piped from curl)
  local v; read -r -p "$1" v < /dev/tty || die "No terminal available for input."
  printf -v "$2" '%s' "$v"
}

[ "$(id -u)" -eq 0 ] || die "Run as root: sudo bash setup.sh"
command -v systemctl >/dev/null || die "systemd is required."

say "Installing system packages"
if command -v apt-get >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y python3 python3-venv python3-pip git cron
  systemctl enable --now cron >/dev/null 2>&1 || true
else
  die "apt-get not found. Install python3, python3-venv, pip, git manually, then re-run."
fi

say "Creating user '${BOT_USER}' (if missing)"
id "$BOT_USER" >/dev/null 2>&1 || adduser --disabled-password --gecos "" "$BOT_USER"

say "Getting the code into ${APP_DIR}"
copy_local() {
  mkdir -p "$APP_DIR"
  tar -C "$SRC_DIR" --exclude='./.env' --exclude='./*.db*' --exclude='./.venv' \
      --exclude='./backups' --exclude='./__pycache__' --exclude='./.git' -cf - . | tar -C "$APP_DIR" -xf -
}
# If run from a git checkout, reuse its origin so update.sh can "git pull" later.
if [ -z "${REPO_URL:-}" ] && [ -d "$SRC_DIR/.git" ]; then
  REPO_URL="$(git -C "$SRC_DIR" remote get-url origin 2>/dev/null || true)"
fi
if [ "$SRC_DIR" = "$APP_DIR" ]; then
  :  # already in place
elif [ -d "$APP_DIR/.git" ] && [ -n "${REPO_URL:-}" ]; then
  runuser -u "$BOT_USER" -- git -C "$APP_DIR" pull --ff-only
elif [ -n "${REPO_URL:-}" ] && [ ! -d "$APP_DIR" ] && \
     GIT_TERMINAL_PROMPT=0 runuser -u "$BOT_USER" -- git clone "$REPO_URL" "$APP_DIR"; then
  :  # cloned
else
  [ -n "${REPO_URL:-}" ] && echo "Could not clone (private repo?). Copying local files instead; update.sh won't work until you clone manually."
  copy_local
fi
chown -R "$BOT_USER:$BOT_USER" "$APP_DIR"
chmod +x "$APP_DIR"/*.sh

say "Setting up Python environment"
runuser -u "$BOT_USER" -- bash -c "cd '$APP_DIR' && python3 -m venv .venv && .venv/bin/pip install --upgrade pip -q && .venv/bin/pip install -r requirements.txt -q"

if [ -f "$APP_DIR/.env" ]; then
  say "Keeping existing .env"
else
  say "Configuring the bot"
  BOT_TOKEN=""; ADMIN_IDS=""
  while ! [[ "$BOT_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]]; do
    ask "Bot token from @BotFather: " BOT_TOKEN
    [[ "$BOT_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]] || echo "That doesn't look like a bot token, try again."
  done
  while ! [[ "$ADMIN_IDS" =~ ^[0-9]+(,[0-9]+)*$ ]]; do
    ask "Your Telegram user ID(s), comma separated (get it from @userinfobot): " ADMIN_IDS
    ADMIN_IDS="${ADMIN_IDS// /}"
    [[ "$ADMIN_IDS" =~ ^[0-9]+(,[0-9]+)*$ ]] || echo "Numbers only, e.g. 123456789 or 111,222."
  done
  cat > "$APP_DIR/.env" <<ENV
BOT_TOKEN=${BOT_TOKEN}
ADMIN_IDS=${ADMIN_IDS}
ALLOWED_CHAT_IDS=
POLL_MINUTES=20
MAX_FOLLOWS=20
NOTIFY_ON_START=1
ENV
  chown "$BOT_USER:$BOT_USER" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
fi

say "Installing systemd service"
cat > "/etc/systemd/system/${SERVICE}.service" <<UNIT
[Unit]
Description=Pocket FM episode alert bot
After=network-online.target
Wants=network-online.target

[Service]
User=${BOT_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=${APP_DIR}/.venv/bin/python pocketfm_bot.py
Restart=always
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null
systemctl restart "$SERVICE"

say "Scheduling daily database backup (3:00 AM)"
CRON_LINE="0 3 * * * ${APP_DIR}/backup.sh"
( crontab -u "$BOT_USER" -l 2>/dev/null | grep -vF "${APP_DIR}/backup.sh" || true; echo "$CRON_LINE" ) | crontab -u "$BOT_USER" -

sleep 3
say "Done"
systemctl --no-pager --lines=8 status "$SERVICE" || true
cat <<MSG

Bot is running. Useful commands:
  journalctl -u ${SERVICE} -f        live logs
  systemctl restart ${SERVICE}       restart
  systemctl stop ${SERVICE}          stop
  sudo bash ${APP_DIR}/update.sh     update from GitHub
Config file: ${APP_DIR}/.env   (edit, then restart)
Open your bot in Telegram and send /adminhelp to test.
MSG
