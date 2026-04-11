# mcp-server-v2 Windows Service登録スクリプト
# 管理者権限で実行すること

$serviceName = "MirageV2"
$scriptPath  = "C:\MirageWork\mcp-server-v2\start_server.bat"

# 既存サービスを削除
if (Get-Service -Name $serviceName -ErrorAction SilentlyContinue) {
    Stop-Service -Name $serviceName -Force
    sc.exe delete $serviceName
    Start-Sleep 2
}

# NSSMでサービス登録（既存のmcp-serverと同じ方式）
$nssm = "C:\MirageWork\tools\nssm.exe"
if (Test-Path $nssm) {
    & $nssm install $serviceName "C:\Windows\System32\cmd.exe" "/c `"$scriptPath`""
    & $nssm set $serviceName AppDirectory "C:\MirageWork\mcp-server-v2"
    & $nssm set $serviceName DisplayName "MirageSystem MCP Server v2"
    & $nssm set $serviceName Start SERVICE_AUTO_START
    Start-Service -Name $serviceName
    Write-Host "Service $serviceName started."
} else {
    Write-Host "NSSM not found. Starting as background process..."
    Start-Process "C:\Windows\py.exe" -ArgumentList "C:\MirageWork\mcp-server-v2\server.py" -WindowStyle Hidden
    Write-Host "mcp-server-v2 started on port 3001."
}
