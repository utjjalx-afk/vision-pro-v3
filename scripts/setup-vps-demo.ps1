$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
        & py -3.11 -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 is required.' }
    }
    & ./.venv/Scripts/python.exe -m pip install -c requirements-dashboard.lock -e '.[dashboard,mt5]'
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    New-Item -ItemType Directory -Force -Path 'data/private' | Out-Null
    & ./.venv/Scripts/python.exe -c "import secrets; from pathlib import Path; [(p.write_text(secrets.token_urlsafe(48), encoding='utf-8') if not p.exists() else None) for p in [Path('data/private/viewer.token'),Path('data/private/bridge.token')]]"
    if ($LASTEXITCODE -ne 0) { throw 'Local key initialization failed.' }
    $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    & icacls.exe 'data' /inheritance:r /grant:r "${user}:(OI)(CI)F" 'SYSTEM:(OI)(CI)F' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Private directory ACL setup failed.' }
    if (-not (Test-Path -LiteralPath 'apps/dashboard/dist/index.html')) {
        throw 'Built dashboard is missing. Build apps/dashboard with npm ci and npm run build.'
    }
    Write-Output 'Dependencies and separate private keys ready. No terminal settings changed; no trades placed.'
} finally { Pop-Location }
