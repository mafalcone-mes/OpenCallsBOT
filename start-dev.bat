@echo off
rem Opens the board with the developer view: a right-hand bar with system
rem state (Ollama on GPU or CPU, NVIDIA driver, storage), hidden actions
rem (update Ollama, rescore, clear caches...), dev details on every card, logs.
rem   start-dev.bat          the usual board (data\)          http://127.0.0.1:8767
rem   start-dev.bat design   the design board (data-design\)  http://127.0.0.1:8768
rem Runs next to start.bat / start-design.bat without disturbing them.
cd /d "%~dp0"

powershell -NoProfile -Command "try { Invoke-WebRequest http://127.0.0.1:11434/api/version -UseBasicParsing -TimeoutSec 2 | Out-Null; exit 0 } catch { exit 1 }"
if errorlevel 1 (
  echo Starting Ollama...
  if not exist "data\logs" mkdir "data\logs"
  start "Ollama" /min cmd /c ""%LOCALAPPDATA%\Programs\Ollama\ollama.exe" serve >> "%~dp0data\logs\ollama.log" 2>&1"
  timeout /t 4 /nobreak >nul
)

set VS_DEV=1
if /i "%~1"=="design" (
  set VS_DATA_DIR=data-design
  set VS_SOURCES_CSV=sources-design.csv
  uv run vs serve --port 8768
) else (
  uv run vs serve --port 8767
)
