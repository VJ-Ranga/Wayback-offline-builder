#!/usr/bin/env bash
set -euo pipefail

echo
echo "==============================================="
echo "  VJRanga - Wayback Offline Builder Installer"
echo "==============================================="
echo
echo "[1/4] Preparing Python environment..."

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

.venv/bin/python -m pip install --disable-pip-version-check -q --upgrade pip
.venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt

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

echo "[2/4] Writing local configuration (.env)..."

if [ "$db_choice" = "mysql" ]; then
  echo "[3/4] Ensuring MySQL database exists..."
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

  .venv/bin/python - "$mysql_host" "$mysql_port" "$mysql_user" "$mysql_pass" "$mysql_db" <<'PY'
import sys
import pymysql

host = sys.argv[1]
port = int(sys.argv[2])
user = sys.argv[3]
password = sys.argv[4]
database = sys.argv[5].replace("`", "")

conn = pymysql.connect(host=host, port=port, user=user, password=password, charset="utf8mb4", autocommit=True)
cur = conn.cursor()
cur.execute(f"CREATE DATABASE IF NOT EXISTS `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
cur.close()
conn.close()
PY
  echo "MySQL database ensured: $mysql_db"
else
  echo "[3/4] Initializing SQLite path..."
  mkdir -p "$(dirname "$sqlite_path")"
  touch "$sqlite_path"
fi

echo "[4/4] Install complete."
echo "Run ./run.sh to start the app."
echo "Run ./stop.sh to stop the app."
