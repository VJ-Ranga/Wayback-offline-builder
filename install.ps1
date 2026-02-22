$ErrorActionPreference = "Stop"

if (-not (Test-Path ".venv")) {
  python -m venv .venv
}

& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt

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

if ($dbChoice -eq "mysql") {
  Write-Host "Configured mysql settings in .env. Current app runtime still uses sqlite fallback until mysql store is implemented." -ForegroundColor Yellow
}

Write-Host "Install complete. Run .\run.bat to start the app."
