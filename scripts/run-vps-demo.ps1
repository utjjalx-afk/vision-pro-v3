param(
    [Parameter(Mandatory=$true)][string]$OperationsConfig,
    [Parameter(Mandatory=$true)][string]$TerminalPath,
    [Parameter(Mandatory=$true)][string]$BridgeTokenFile,
    [Parameter(Mandatory=$true)][string]$ViewerTokenFile,
    [string]$DemoConfig,
    [string]$Journal = 'data/private/demo.sqlite',
    [string]$LogDirectory = 'data/vps-observations',
    [switch]$EnableDemo
)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
$children = @()
try {
    $pythonPath = Join-Path $repoRoot '.venv/Scripts/python.exe'
    foreach ($required in @($pythonPath, $OperationsConfig, $TerminalPath, $BridgeTokenFile, $ViewerTokenFile)) {
        if (-not (Test-Path -LiteralPath $required -PathType Leaf)) { throw 'Required local file missing.' }
    }
    $bridgePath = (Resolve-Path -LiteralPath $BridgeTokenFile).Path
    $viewerPath = (Resolve-Path -LiteralPath $ViewerTokenFile).Path
    if ($bridgePath -eq $viewerPath -or
        (Get-Content -Raw -LiteralPath $bridgePath).Trim() -eq (Get-Content -Raw -LiteralPath $viewerPath).Trim()) {
        throw 'Viewer and broker keys must be distinct.'
    }
    $operations = Get-Content -Raw -LiteralPath $OperationsConfig | ConvertFrom-Json
    if ($operations.dashboard_port -ne 8787 -or
        (Resolve-Path -LiteralPath $operations.viewer_token_file).Path -ne $viewerPath) {
        throw 'Operations configuration must match the local viewer and port 8787.'
    }
    $terminals = @(Get-CimInstance Win32_Process -Filter "Name='terminal64.exe'")
    if ($terminals.Count -ne 1 -or $terminals[0].ExecutablePath -ne (Resolve-Path -LiteralPath $TerminalPath).Path) {
        throw 'Exactly one already connected MT5 terminal required.'
    }
    foreach ($port in @(8787,8766)) {
        if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) {
            throw "Port $port is occupied; no existing process will be stopped."
        }
    }
    if ($EnableDemo -and -not $DemoConfig) { throw 'Explicit private demo configuration required.' }
    if ($DemoConfig) {
        $demo = Get-Content -Raw -LiteralPath $DemoConfig | ConvertFrom-Json
        if ($demo.demo_policy.account_identity -ne $operations.account_identity -or
            [IO.Path]::GetFullPath($operations.journal) -ne [IO.Path]::GetFullPath($Journal)) {
            throw 'Journal and pinned demo identity must agree.'
        }
        if ($EnableDemo -and $demo.demo_policy.phase11_accepted -ne $true) {
            throw 'Phase 11 acceptance required; no acceptance is inferred.'
        }
        if ($EnableDemo -and $demo.demo_policy.require_tp -ne $true) {
            throw 'This VPS experiment requires mandatory TP as well as SL.'
        }
    } elseif ($operations.journal) { throw 'Journal configured without a demo gateway.' }
    New-Item -ItemType Directory -Force -Path $LogDirectory | Out-Null
    function Start-OwnedPython([string]$name, [string[]]$arguments) {
        $stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfff')
        # Start-Process joins arguments; quote paths without shell evaluation.
        $quoted = @($arguments | ForEach-Object {
            if ($_ -match '["\r\n]') { throw 'Unsupported argument characters.' }
            '"' + $_ + '"'
        })
        Start-Process -FilePath $pythonPath -ArgumentList $quoted -WorkingDirectory $repoRoot -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $LogDirectory "$stamp-$name.stdout.log") `
            -RedirectStandardError (Join-Path $LogDirectory "$stamp-$name.stderr.log") -PassThru
    }
    if ($DemoConfig) {
        $env:MT5_BRIDGE_TOKEN = (Get-Content -Raw -LiteralPath $bridgePath).Trim()
        $gatewayArguments = @('-m','vision.execution.demo.server','--config',$DemoConfig,
            '--terminal-path',$TerminalPath,'--journal',$Journal,'--port','8766')
        if ($EnableDemo) { $gatewayArguments += '--enable-demo' }
        $children += Start-OwnedPython 'gateway' $gatewayArguments
        Remove-Item Env:MT5_BRIDGE_TOKEN
    }
    $children += Start-OwnedPython 'dashboard' @('-m','vision.apps.dashboard.server',
        '--token-file',$viewerPath,'--mt5-terminal',$TerminalPath,'--bridge-token-file',$bridgePath)
    $children += Start-OwnedPython 'collector' @('-m','vision.operations.demo','collect',
        '--config',$OperationsConfig,'--directory',$LogDirectory)
    Write-Output 'VPS DEMO processes started; gateway startup is DISARMED. Logs remain private.'
    # A failed child stops the group; a scheduled task can restart it DISARMED.
    while ($true) {
        foreach ($child in $children) {
            $child.Refresh()
            if ($child.HasExited) { throw 'A child stopped. Review private logs before restarting.' }
        }
        Start-Sleep -Seconds 5
    }
} finally {
    Remove-Item Env:MT5_BRIDGE_TOKEN -ErrorAction SilentlyContinue
    foreach ($child in $children) {
        $child.Refresh()
        if (-not $child.HasExited) {
            & taskkill.exe /PID $child.Id /T /F | Out-Null
        }
    }
    Pop-Location
}
