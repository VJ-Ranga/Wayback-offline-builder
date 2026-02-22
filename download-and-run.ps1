$ErrorActionPreference = "Stop"

$RepoOwner = "VJ-Ranga"
$RepoName = "Wayback-offline-builder"
$Ref = "main"
$TargetDir = "wayback-offline-builder"

Write-Host ""
Write-Host "===============================================" -ForegroundColor DarkYellow
Write-Host "  VJRanga - Wayback Offline Builder" -ForegroundColor Yellow
Write-Host "===============================================" -ForegroundColor DarkYellow
Write-Host ""

if (Test-Path $TargetDir) {
  throw "Target directory '$TargetDir' already exists. Use -TargetDir <new-path> or remove existing directory."
}

$zipUrl = "https://github.com/$RepoOwner/$RepoName/archive/refs/heads/$Ref.zip"
$tmpRoot = Join-Path $env:TEMP ("wob-" + [guid]::NewGuid().ToString("N"))
$zipPath = Join-Path $tmpRoot "source.zip"
$extractRoot = Join-Path $tmpRoot "extract"

New-Item -ItemType Directory -Path $tmpRoot -Force | Out-Null
New-Item -ItemType Directory -Path $extractRoot -Force | Out-Null

try {
  Write-Host "[1/3] Downloading $RepoName ($Ref)..." -ForegroundColor Cyan
  Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath -UseBasicParsing

  Expand-Archive -LiteralPath $zipPath -DestinationPath $extractRoot -Force
  $srcDir = Join-Path $extractRoot "$RepoName-$Ref"
  if (-not (Test-Path $srcDir)) {
    throw "Could not find extracted source directory: $srcDir"
  }

  Move-Item -Path $srcDir -Destination $TargetDir
  Set-Location $TargetDir

  Write-Host "[2/3] Project extracted to: $TargetDir" -ForegroundColor Cyan
  & .\install.ps1

  Write-Host "[3/3] Starting app..." -ForegroundColor Cyan
  & .\run.bat
}
finally {
  if (Test-Path $tmpRoot) {
    Remove-Item -Path $tmpRoot -Recurse -Force -ErrorAction SilentlyContinue
  }
}
