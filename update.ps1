$ErrorActionPreference = "Stop"

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
  throw "git is required to update this project."
}

$dirty = git status --porcelain
if ($dirty) {
  throw "Working tree has local changes. Commit or stash before update."
}

$envPath = ".env"
$dbPath = "archive_cache.sqlite3"
if (Test-Path $envPath) {
  $line = Select-String -Path $envPath -Pattern "^SQLITE_DB_PATH=" -SimpleMatch:$false | Select-Object -First 1
  if ($line) {
    $dbPath = $line.ToString().Split("=", 2)[1]
  }
}

if (Test-Path $dbPath) {
  $backupDir = Join-Path "runtime" "db-backups"
  New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
  $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
  Copy-Item $dbPath (Join-Path $backupDir "archive_cache-$stamp.sqlite3") -Force
  Write-Host "Backed up DB to $backupDir"
}

git pull --ff-only

if (-not (Test-Path ".venv")) {
  python -m venv .venv
}

& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt

Write-Host "Update complete. Restart app using .\run.bat"
