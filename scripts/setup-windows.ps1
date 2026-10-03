$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $repoRoot
try {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Virtual environment setup failed' }
    & (Join-Path $repoRoot '.venv\Scripts\python.exe') -m pip install -e '.[dev]'
    if ($LASTEXITCODE -ne 0) { throw 'Package installation failed' }
} finally {
    Pop-Location
}
