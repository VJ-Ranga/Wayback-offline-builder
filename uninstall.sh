#!/usr/bin/env bash
set -euo pipefail

confirm() {
  local prompt="$1"
  local answer=""
  read -r -p "$prompt (y/N): " answer
  [ "${answer,,}" = "y" ]
}

get_env_value() {
  local key="$1"
  if [ ! -f ".env" ]; then
    return 1
  fi
  local line=""
  line="$(grep -E "^${key}=" .env | head -n 1 || true)"
  if [ -z "$line" ]; then
    return 1
  fi
  printf '%s' "${line#*=}"
}

if confirm "Remove virtual environment (.venv)?"; then
  rm -rf .venv
fi

if confirm "Remove runtime files (runtime/, logs, pid)?"; then
  rm -rf runtime
fi

if confirm "Remove local config (.env)?"; then
  rm -f .env
fi

if confirm "Remove SQLite database file? (uses SQLITE_DB_PATH from .env if present)"; then
  sqlite_path="$(get_env_value SQLITE_DB_PATH || echo "archive_cache.sqlite3")"
  rm -f "$sqlite_path"
fi

if confirm "Remove output files folder? (uses OUTPUT_ROOT_DIR from .env if present)"; then
  output_root="$(get_env_value OUTPUT_ROOT_DIR || echo "output")"
  rm -rf "$output_root"
fi

echo "Uninstall cleanup finished."
