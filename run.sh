#!/usr/bin/env bash
set -euo pipefail

if [ ! -x ".venv/bin/python" ]; then
  echo "Virtual environment not found. Run ./install.sh first."
  exit 1
fi

.venv/bin/python run_and_healthcheck.py --open-browser
echo
echo "App is running in background."
echo "Stop anytime with: ./stop.sh"
