# register_v2_task.ps1
# MirageMCPServerV2 タスクを jun + 最高権限で登録する
# 管理者 PowerShell から実行すること

$taskName = "MirageMCPServerV2"
$action = New-ScheduledTaskAction `
    -Execute "C:\MirageWork\mcp-server-v2\start_v2_jun.bat"

$trigger = New-ScheduledTaskTrigger -AtStartup

$principal = New-ScheduledTaskPrincipal `
    -UserId "BOSGAME-M31G9\jun" `
    -LogonType Interactive `
    -RunLevel Highest

$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 3) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Force

Write-Host "Registered: $taskName"
Write-Host "Run: Start-ScheduledTask -TaskName '$taskName'"
