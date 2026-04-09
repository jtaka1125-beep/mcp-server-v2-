@echo off
REM mcp-server-v2 起動スクリプト（重複起動防止付き）

cd /d C:\MirageWork\mcp-server-v2

REM 既存プロセスチェック（PIDファイル確認）
if exist server.pid (
    for /f %%p in (server.pid) do (
        tasklist /FI "PID eq %%p" 2>nul | find "python" >nul
        if not errorlevel 1 (
            echo [%date% %time%] Server already running (PID %%p). Exiting.
            exit /b 0
        )
    )
    del server.pid 2>nul
)

echo [%date% %time%] Starting mcp-server-v2...
C:\Windows\py.exe server.py
echo [%date% %time%] Server exited (rc=%errorlevel%)
