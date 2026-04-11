@echo off
REM mcp-server-v2 起動スクリプト（jun権限版）

for /f "tokens=3" %%i in ('reg query HKCU\Environment /v CEREBRAS_API_KEY 2^>nul') do set CEREBRAS_API_KEY=%%i
for /f "tokens=3" %%i in ('reg query HKCU\Environment /v GROQ_API_KEY 2^>nul') do set GROQ_API_KEY=%%i
for /f "tokens=3" %%i in ('reg query HKCU\Environment /v GEMINI_API_KEY 2^>nul') do set GEMINI_API_KEY=%%i
for /f "tokens=3" %%i in ('reg query HKCU\Environment /v GEMINI_API_KEY 2^>nul') do set GEMINI_API_KEY=%%i

set PATH=C:\Users\jun\.local\bin;%PATH%

cd /d C:\MirageWork\mcp-server-v2
C:\Windows\py.exe server.py
