#!/usr/bin/env bash
# One-step setup for the Pocket FM alert bot.
# Usage:  bash setup.sh        (installs + asks for token)
#         bash run.sh          (starts the bot afterwards)
set -e
cd "$(dirname "$0")"

PY=$(command -v python3 || command -v python || true)
if [ -z "$PY" ]; then
  echo "Python 3 not found. Install it from https://www.python.org and re-run." >&2
  exit 1
fi

echo "Creating virtual environment..."
$PY -m venv .venv
source .venv/bin/activate

echo "Installing dependencies..."
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

touch .env
chmod 600 .env

# Ask only for settings that are missing, so re-running after an update fills in new ones.
ask() {  # ask KEY "prompt" [default]
  local key="$1" prompt="$2" default="$3" val
  if ! grep -q "^${key}=." .env; then
    if [ -n "$default" ]; then
      read -rp "$prompt [$default]: " val
      val="${val:-$default}"
    else
      read -rp "$prompt: " val
    fi
    sed -i "/^${key}=/d" .env
    echo "${key}=${val}" >> .env
  fi
}

ask BOT_TOKEN "Paste your BotFather token"
ask ADMIN_IDS "Your numeric Telegram user ID (from @userinfobot, for admin commands)"
ask FORCE_SUB_CHANNEL "Channel users must join (@username, make the bot an admin there; leave blank to disable)" "@audios_fm"
echo "Settings saved in .env (git-ignored)."

cat > run.sh << 'RUN'
#!/usr/bin/env bash
cd "$(dirname "$0")"
source .venv/bin/activate
set -a; source .env; set +a
exec python pocketfm_bot.py
RUN
chmod +x run.sh

echo
echo "Done. Start the bot with:  bash run.sh"
echo "Keep it running in the background with:  nohup bash run.sh > bot.log 2>&1 &"
