#!/usr/bin/env bash
# Safe online backup of the SQLite database. Keeps the last 14 backups.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$DIR/backups"
[ -f "$DIR/pocketfm_bot.db" ] || { echo "No database yet, nothing to back up."; exit 0; }
DIR="$DIR" python3 - <<'PY'
import os, sqlite3, time
d = os.environ["DIR"]
src = sqlite3.connect(os.path.join(d, "pocketfm_bot.db"))
dst = sqlite3.connect(os.path.join(d, "backups", "pocketfm_bot-" + time.strftime("%Y%m%d-%H%M%S") + ".db"))
src.backup(dst); dst.close(); src.close()
PY
ls -1t "$DIR"/backups/*.db | tail -n +15 | xargs -r rm --
