@echo off
cd /d "%~dp0"
python -m screen_typer --english
if errorlevel 1 pause
