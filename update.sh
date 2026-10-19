#!/usr/bin/env bash
# Pull the latest code from GitHub, update dependencies and restart the bot. Run as root.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OWNER="$(stat -c '%U' "$DIR")"
[ "$(id -u)" -eq 0 ] || { echo "Run as root: sudo bash update.sh"; exit 1; }
[ -d "$DIR/.git" ] || { echo "Not a git checkout. Re-run setup.sh with REPO_URL=<your repo url>."; exit 1; }
runuser -u "$OWNER" -- git -C "$DIR" pull --ff-only
runuser -u "$OWNER" -- "$DIR/.venv/bin/pip" install -r "$DIR/requirements.txt" -q
systemctl restart pocketfm-bot
sleep 2
systemctl --no-pager --lines=5 status pocketfm-bot || true
