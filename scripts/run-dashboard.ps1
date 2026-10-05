param([switch]$PublicFeed, [switch]$Mt5Demo, [string]$BridgeTokenFile = 'data/private/bridge.token', [int]$Port = 8787)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    $pythonPath = Join-Path $repoRoot '.venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Run setup-windows first.' }
    $feedName = if ($PublicFeed) { 'binance' } else { 'off' }
    $dashboardArgs = @('-m', 'vision.apps.dashboard.server', '--port', "$Port", '--token-file', 'data/private/viewer.token', '--feed', $feedName)
    if ($Mt5Demo) {
        $terminals = @(Get-CimInstance Win32_Process -Filter "Name='terminal64.exe'" | Select-Object -ExpandProperty ExecutablePath -Unique)
        if ($terminals.Count -ne 1 -or -not (Test-Path -LiteralPath $terminals[0])) { throw 'Open exactly one connected MT5 DEMO terminal.' }
        if (-not (Test-Path -LiteralPath $BridgeTokenFile)) { throw 'Existing separate private bridge identity key required. Viewer token is never reused.' }
        $dashboardArgs += @('--mt5-terminal', $terminals[0], '--bridge-token-file', $BridgeTokenFile)
    }
    & $pythonPath @dashboardArgs
    if ($LASTEXITCODE -ne 0) { throw 'Dashboard failed.' }
} finally { Pop-Location }
