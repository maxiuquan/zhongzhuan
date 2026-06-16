@echo off
REM Green version: register user-level auto-start (HKCU Run)
set "EXE=%~dp0zhongzhuan.exe"
if not exist "%EXE%" (
    echo Error: zhongzhuan.exe not found in current directory
    pause
    exit /b 1
)
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "Zhongzhuan" /t REG_SZ /d "\"%EXE%\"" /f
echo Zhongzhuan registered for user auto-start.
echo To uninstall, run: green-uninstall.bat
pause