@echo off
rem Opens the opportunities dashboard. Starts Ollama first if it isn't running.
cd /d "%~dp0"

powershell -NoProfile -Command "try { Invoke-WebRequest http://127.0.0.1:11434/api/version -UseBasicParsing -TimeoutSec 2 | Out-Null; exit 0 } catch { exit 1 }"
if errorlevel 1 (
  echo Starting Ollama...
  rem its messages go to data\logs\ollama.log (read by the developer view)
  if not exist "data\logs" mkdir "data\logs"
  start "Ollama" /min cmd /c ""%LOCALAPPDATA%\Programs\Ollama\ollama.exe" serve >> "%~dp0data\logs\ollama.log" 2>&1"
  timeout /t 4 /nobreak >nul
)

uv run vs serve
