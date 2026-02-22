#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"
pid_file="$script_dir/runtime/server.pid"

if [ ! -f "$pid_file" ]; then
  echo "No pid file found. Server may already be stopped."
  exit 0
fi

pid="$(tr -d '[:space:]' < "$pid_file")"
if [ -z "$pid" ]; then
  echo "PID file is empty."
  exit 0
fi

if kill -0 "$pid" >/dev/null 2>&1; then
  kill "$pid"
  echo "Stopped server PID: $pid"
else
  echo "Process $pid is not running."
fi
