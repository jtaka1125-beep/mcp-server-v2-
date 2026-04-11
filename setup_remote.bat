@echo off
REM mcp-server-v2 git remote 設定 & push
REM GitHub で mcp-server-v2 リポジトリ作成後に実行

cd /d C:\MirageWork\mcp-server-v2

git remote add origin git@github.com:jtaka1125-beep/mcp-server-v2.git
git push -u origin master

echo.
echo Done. Check above for errors.
pause
