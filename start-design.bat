@echo off
rem Opens the second, design-focused board: its own data (data-design\) and the
rem sources in sources-design.csv instead of the Google Sheet. The usual board
rem (start.bat, data\) is not touched. Both can run at the same time.
cd /d "%~dp0"

powershell -NoProfile -Command "try { Invoke-WebRequest http://127.0.0.1:11434/api/version -UseBasicParsing -TimeoutSec 2 | Out-Null; exit 0 } catch { exit 1 }"
if errorlevel 1 (
  echo Starting Ollama...
  start "" /min "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" serve
  timeout /t 4 /nobreak >nul
)

set VS_DATA_DIR=data-design
set VS_SOURCES_CSV=sources-design.csv
uv run vs serve --port 8766
