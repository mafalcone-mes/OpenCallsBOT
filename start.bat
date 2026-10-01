@echo off
rem Opens the opportunities dashboard. Starts Ollama first if it isn't running.
cd /d "%~dp0"

powershell -NoProfile -Command "try { Invoke-WebRequest http://127.0.0.1:11434/api/version -UseBasicParsing -TimeoutSec 2 | Out-Null; exit 0 } catch { exit 1 }"
if errorlevel 1 (
  echo Starting Ollama...
  start "" /min "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" serve
  timeout /t 4 /nobreak >nul
)

uv run vs serve
