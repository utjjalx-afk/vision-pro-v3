$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) { throw 'Run setup-windows.ps1 first' }
Push-Location -LiteralPath $repoRoot
try {
    & $venvPython -m ruff check .
    if ($LASTEXITCODE -ne 0) { throw 'Lint failed' }
    & $venvPython -m ruff format --check .
    if ($LASTEXITCODE -ne 0) { throw 'Formatting failed' }
    & $venvPython -m pytest
    if ($LASTEXITCODE -ne 0) { throw 'Tests failed' }
} finally {
    Pop-Location
}
