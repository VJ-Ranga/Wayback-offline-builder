#!/usr/bin/env bash
set -euo pipefail

if ! command -v git >/dev/null 2>&1; then
  echo "git is required to update this project."
  exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "Working tree has local changes. Commit or stash before update."
  exit 1
fi

db_path="archive_cache.sqlite3"
if [ -f ".env" ]; then
  env_db="$(grep -E '^SQLITE_DB_PATH=' .env | head -n 1 | cut -d'=' -f2- || true)"
  if [ -n "$env_db" ]; then
    db_path="$env_db"
  fi
fi

if [ -f "$db_path" ]; then
  mkdir -p runtime/db-backups
  stamp="$(date +%Y%m%d-%H%M%S)"
  cp "$db_path" "runtime/db-backups/archive_cache-$stamp.sqlite3"
  echo "Backed up DB to runtime/db-backups"
fi

git pull --ff-only

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

echo "Update complete. Restart app using ./run.sh"
