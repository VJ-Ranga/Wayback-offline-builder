#!/usr/bin/env bash
set -euo pipefail

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

ask_value() {
  local prompt="$1"
  local default_value="$2"
  local input=""
  read -r -p "$prompt [$default_value]: " input
  if [ -z "$input" ]; then
    printf '%s' "$default_value"
  else
    printf '%s' "$input"
  fi
}

db_choice="$(ask_value "Select DB backend: sqlite or mysql" "sqlite")"
db_choice="${db_choice,,}"
if [ "$db_choice" != "sqlite" ] && [ "$db_choice" != "mysql" ]; then
  db_choice="sqlite"
fi

data_root="$(ask_value "Data folder location" "./data")"
mkdir -p "$data_root"

sqlite_default="$data_root/archive_cache.sqlite3"
sqlite_path="$(ask_value "SQLite file path" "$sqlite_default")"

output_root="$(ask_value "Output folder location" "./output")"
mkdir -p "$output_root"

{
  echo "DB_BACKEND=$db_choice"
  echo "SQLITE_DB_PATH=$sqlite_path"
  echo "OUTPUT_ROOT_DIR=$output_root"
  echo "HOST=127.0.0.1"
  echo "PORT=5000"
} > .env

if [ "$db_choice" = "mysql" ]; then
  mysql_host="$(ask_value "MySQL host" "127.0.0.1")"
  mysql_port="$(ask_value "MySQL port" "3306")"
  mysql_db="$(ask_value "MySQL database name" "wayback_builder")"
  mysql_user="$(ask_value "MySQL username" "root")"
  read -r -p "MySQL password (saved in .env): " mysql_pass

  {
    echo "MYSQL_HOST=$mysql_host"
    echo "MYSQL_PORT=$mysql_port"
    echo "MYSQL_DATABASE=$mysql_db"
    echo "MYSQL_USER=$mysql_user"
    echo "MYSQL_PASSWORD=$mysql_pass"
  } >> .env

  echo "Configured mysql settings in .env. Current app runtime still uses sqlite fallback until mysql store is implemented."
fi

echo "Install complete. Run ./run.sh to start the app."
