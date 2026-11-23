@echo off
rem Installs the Motic camera driver from the "driver" folder (asks for administrator rights).
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_driver.ps1" %*
