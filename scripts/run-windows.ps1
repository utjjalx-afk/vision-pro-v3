$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) { throw 'Run setup-windows.ps1 first' }
& $venvPython -m vision @args
exit $LASTEXITCODE
