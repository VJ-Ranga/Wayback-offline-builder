$ErrorActionPreference = "Stop"

function Confirm-Action([string]$Message) {
  $answer = Read-Host "$Message (y/N)"
  return $answer.Trim().ToLower() -eq "y"
}

if (Confirm-Action "Remove virtual environment (.venv)?") {
  if (Test-Path ".venv") { Remove-Item ".venv" -Recurse -Force }
}

if (Confirm-Action "Remove runtime files (runtime/, logs, pid)?") {
  if (Test-Path "runtime") { Remove-Item "runtime" -Recurse -Force }
}

if (Confirm-Action "Remove local config (.env)?") {
  if (Test-Path ".env") { Remove-Item ".env" -Force }
}

if (Confirm-Action "Remove SQLite database file? (uses SQLITE_DB_PATH from .env if present)") {
  $dbPath = "archive_cache.sqlite3"
  if (Test-Path ".env") {
    $line = Select-String -Path ".env" -Pattern "^SQLITE_DB_PATH=" -SimpleMatch:$false | Select-Object -First 1
    if ($line) {
      $dbPath = $line.ToString().Split("=", 2)[1]
    }
  }
  if (Test-Path $dbPath) { Remove-Item $dbPath -Force }
}

if (Confirm-Action "Remove output files folder (OUTPUT_ROOT_DIR from .env if present)?") {
  $outputRoot = "output"
  if (Test-Path ".env") {
    $line = Select-String -Path ".env" -Pattern "^OUTPUT_ROOT_DIR=" -SimpleMatch:$false | Select-Object -First 1
    if ($line) {
      $outputRoot = $line.ToString().Split("=", 2)[1]
    }
  }
  if (Test-Path $outputRoot) { Remove-Item $outputRoot -Recurse -Force }
}

Write-Host "Uninstall cleanup finished."
