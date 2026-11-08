#!/usr/bin/env bash
# Start Healthbox on the real box: checks the network, tray camera and motor board first.
#   ./run_box.sh            check, then start
#   ./run_box.sh --check    check only
set -euo pipefail
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || { echo "Run ./run.sh once first, to set up Python."; exit 1; }
exec .venv/bin/python scripts/start_box.py "$@"
