# register_guard_task.ps1
# MirageMCPGuardV2 タスクを jun + 最高権限で登録する
# 管理者 PowerShell から実行すること

$taskName = "MirageMCPGuardV2"
$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -File C:\MirageWork\mcp-server-v2\mcp_guard_v2.ps1"

$trigger = New-ScheduledTaskTrigger -AtStartup

$principal = New-ScheduledTaskPrincipal `
    -UserId "BOSGAME-M31G9\jun" `
    -LogonType Interactive `
    -RunLevel Highest

$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Force

Write-Host "Registered: $taskName"
