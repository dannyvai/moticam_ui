@echo off
rem Builds dist\MoticamCapture (standalone app + Motic SDK DLL + camera driver) and a zip of it.
cd /d "%~dp0"
uv run --group build python scripts\build_standalone.py %*
pause
