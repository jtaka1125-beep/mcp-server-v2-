@echo off
REM mcp-server-v2 起動スクリプト (Windows Python 3.12 MSC 版)
REM usearch / fastembed が Microsoft Store Python でネイティブに動くため切り替え

for /f "tokens=3" %%i in ('reg query HKCU\Environment /v CEREBRAS_API_KEY 2^>nul') do set CEREBRAS_API_KEY=%%i
for /f "tokens=3" %%i in ('reg query HKCU\Environment /v GROQ_API_KEY 2^>nul') do set GROQ_API_KEY=%%i
for /f "tokens=3" %%i in ('reg query HKCU\Environment /v GEMINI_API_KEY 2^>nul') do set GEMINI_API_KEY=%%i

set PATH=C:\Users\jun\.local\bin;%PATH%

cd /d C:\MirageWork\mcp-server-v2
REM py.exe = Python Launcher -> Microsoft Store Python 3.12 (MSC, native wheels)
C:\Windows\py.exe -3.12 server.py
