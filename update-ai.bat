@echo off
rem Updates Ollama, checks the model runs on the GPU (goes back to the last
rem working version if not), then opens the NVIDIA app for the driver.
rem   update-ai.bat            normal update
rem   update-ai.bat -Repair    reinstall Ollama even if it is up to date
rem   update-ai.bat -Restore   put back the last Ollama that worked on the GPU
rem   update-ai.bat -NoNvidia  skip the NVIDIA step
rem Also in the developer view (start-dev.bat): Actions, Update Ollama + NVIDIA.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\update_ai.ps1" %*
