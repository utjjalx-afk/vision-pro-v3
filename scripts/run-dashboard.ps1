param([switch]$PublicFeed, [int]$Port = 8787)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    $pythonPath = Join-Path $repoRoot '.venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Run setup-windows first.' }
    $feedName = if ($PublicFeed) { 'binance' } else { 'off' }
    & $pythonPath -m vision.apps.dashboard.server --port $Port --token-file data/private/viewer.token --feed $feedName
    if ($LASTEXITCODE -ne 0) { throw 'Dashboard failed.' }
} finally { Pop-Location }
