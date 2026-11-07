#!/usr/bin/env bash
# Start DoseGuard: one process on 127.0.0.1:8000. Keep the laptop awake.
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
# caffeinate keeps the Mac awake while the server runs (reminders need it).
exec caffeinate -dims .venv/bin/python -m app.main
