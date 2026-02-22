$ErrorActionPreference = "Stop"

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pidFile = Join-Path $scriptRoot "runtime/server.pid"
if (-not (Test-Path $pidFile)) {
  Write-Host "No pid file found. Server may already be stopped."
  exit 0
}

$pidRawText = (Get-Content $pidFile -Raw).Trim()
if ([string]::IsNullOrWhiteSpace($pidRawText)) {
  Write-Host "PID file is empty."
  exit 0
}

$serverPid = [int]$pidRawText
try {
  Stop-Process -Id $serverPid -Force
  Write-Host "Stopped server PID: $serverPid"
} catch {
  Write-Host "Could not stop PID $serverPid (already stopped or no permission)."
}
