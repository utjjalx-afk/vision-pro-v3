param([Parameter(Mandatory=$true)][string]$LauncherConfig)
$ErrorActionPreference = 'Stop'
$configPath = (Resolve-Path -LiteralPath $LauncherConfig).Path
$startPath = Join-Path $PSScriptRoot 'start-vps-demo-from-config.ps1'
if ($configPath -match '["\r\n]' -or $startPath -match '["\r\n]') { throw 'Unsupported path.' }
$taskName = 'VisionProV3-DemoOperations'
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    throw 'Task already exists; inspect it instead of replacing it automatically.'
}
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument (
    '-NoProfile -WindowStyle Hidden -File "' + $startPath + '" -Config "' + $configPath + '"')
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Description (
    'Read-only observations plus optional guarded DEMO gateway. Restart never arms execution.') | Out-Null
Write-Output 'Task installed for this interactive Windows user. Not started or armed.'
