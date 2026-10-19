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

if [ ! -f .env ]; then
  read -rp "Paste your BotFather token: " TOKEN
  read -rp "Your Telegram user ID (get it from @userinfobot; for admin commands): " ADMIN
  printf 'BOT_TOKEN=%s\nADMIN_IDS=%s\n' "$TOKEN" "$ADMIN" > .env
  chmod 600 .env
  echo "Saved token to .env (git-ignored)."
fi

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
