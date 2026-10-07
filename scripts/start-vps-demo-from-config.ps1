param([Parameter(Mandatory=$true)][string]$Config)
$ErrorActionPreference = 'Stop'
$value = Get-Content -Raw -LiteralPath $Config | ConvertFrom-Json
$allowed = @('OperationsConfig','TerminalPath','BridgeTokenFile','ViewerTokenFile',
    'DemoConfig','Journal','LogDirectory','EnableDemo')
$arguments = @{}
foreach ($property in $value.PSObject.Properties) {
    if ($property.Name -notin $allowed) { throw 'Unknown launcher setting.' }
    $arguments[$property.Name] = $property.Value
}
& (Join-Path $PSScriptRoot 'run-vps-demo.ps1') @arguments
