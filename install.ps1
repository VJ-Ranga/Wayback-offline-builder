$ErrorActionPreference = "Stop"

function Show-Banner {
  Write-Host ""
  Write-Host "===============================================" -ForegroundColor DarkYellow
  Write-Host "  VJRanga - Wayback Offline Builder Installer" -ForegroundColor Yellow
  Write-Host "===============================================" -ForegroundColor DarkYellow
  Write-Host ""
}

Show-Banner
Write-Host "[1/4] Preparing Python environment..."

if (-not (Test-Path ".venv")) {
  python -m venv .venv
}

& .\.venv\Scripts\python.exe -m pip install --disable-pip-version-check --no-python-version-warning -q --upgrade pip
& .\.venv\Scripts\python.exe -m pip install --disable-pip-version-check --no-python-version-warning -q -r requirements.txt

function Ask-Value([string]$Prompt, [string]$Default) {
  $value = Read-Host "$Prompt [$Default]"
  if ([string]::IsNullOrWhiteSpace($value)) {
    return $Default
  }
  return $value
}

$dbChoice = Ask-Value "Select DB backend: sqlite or mysql" "sqlite"
$dbChoice = $dbChoice.Trim().ToLower()
if ($dbChoice -ne "sqlite" -and $dbChoice -ne "mysql") {
  $dbChoice = "sqlite"
}

$dataRoot = Ask-Value "Data folder location" ".\\data"
if (-not (Test-Path $dataRoot)) {
  New-Item -ItemType Directory -Path $dataRoot -Force | Out-Null
}

$sqliteDefault = Join-Path $dataRoot "archive_cache.sqlite3"
$sqlitePath = Ask-Value "SQLite file path" $sqliteDefault

$outputRoot = Ask-Value "Output folder location" ".\\output"
if (-not (Test-Path $outputRoot)) {
  New-Item -ItemType Directory -Path $outputRoot -Force | Out-Null
}

$envLines = @(
  "DB_BACKEND=$dbChoice",
  "SQLITE_DB_PATH=$sqlitePath",
  "OUTPUT_ROOT_DIR=$outputRoot",
  "HOST=127.0.0.1",
  "PORT=5000"
)

if ($dbChoice -eq "mysql") {
  $mysqlHost = Ask-Value "MySQL host" "127.0.0.1"
  $mysqlPort = Ask-Value "MySQL port" "3306"
  $mysqlDb = Ask-Value "MySQL database name" "wayback_builder"
  $mysqlUser = Ask-Value "MySQL username" "root"
  $mysqlPass = Read-Host "MySQL password (saved in .env)"

  $envLines += "MYSQL_HOST=$mysqlHost"
  $envLines += "MYSQL_PORT=$mysqlPort"
  $envLines += "MYSQL_DATABASE=$mysqlDb"
  $envLines += "MYSQL_USER=$mysqlUser"
  $envLines += "MYSQL_PASSWORD=$mysqlPass"
}

Set-Content -Path ".env" -Value ($envLines -join "`n") -Encoding UTF8

Write-Host "[2/4] Writing local configuration (.env)..." -ForegroundColor Cyan

if ($dbChoice -eq "sqlite") {
  Write-Host "[3/4] Initializing SQLite path..." -ForegroundColor Cyan
  $sqliteDir = Split-Path -Parent $sqlitePath
  if ($sqliteDir -and -not (Test-Path $sqliteDir)) {
    New-Item -ItemType Directory -Path $sqliteDir -Force | Out-Null
  }
  if (-not (Test-Path $sqlitePath)) {
    New-Item -ItemType File -Path $sqlitePath -Force | Out-Null
  }
}

if ($dbChoice -eq "mysql") {
  Write-Host "[3/4] Ensuring MySQL database exists..." -ForegroundColor Cyan
  & .\.venv\Scripts\python.exe -c "import sys,pymysql; host=sys.argv[1]; port=int(sys.argv[2]); user=sys.argv[3]; password=sys.argv[4]; db=sys.argv[5]; conn=pymysql.connect(host=host, port=port, user=user, password=password, charset='utf8mb4', autocommit=True); cur=conn.cursor(); cur.execute('CREATE DATABASE IF NOT EXISTS `%s` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci' % db.replace('`','')); cur.close(); conn.close()" $mysqlHost $mysqlPort $mysqlUser $mysqlPass $mysqlDb
  Write-Host "MySQL database ensured: $mysqlDb" -ForegroundColor Green
}

Write-Host "[4/4] Install complete." -ForegroundColor Green
Write-Host "Run .\run.bat to start the app."
Write-Host "Run .\stop.ps1 to stop the app."
