@echo off
cd /d "%~dp0"
python -m screen_typer
if errorlevel 1 pause
