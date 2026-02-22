$ErrorActionPreference = "Stop"

$pidFile = "runtime/server.pid"
if (-not (Test-Path $pidFile)) {
  Write-Host "No pid file found. Server may already be stopped."
  exit 0
}

$pidRaw = (Get-Content $pidFile -Raw).Trim()
if ([string]::IsNullOrWhiteSpace($pidRaw)) {
  Write-Host "PID file is empty."
  exit 0
}

$pid = [int]$pidRaw
try {
  Stop-Process -Id $pid -Force
  Write-Host "Stopped server PID: $pid"
} catch {
  Write-Host "Could not stop PID $pid (already stopped or no permission)."
}
