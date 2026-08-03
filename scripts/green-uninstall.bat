@echo off
REM Green version: remove user-level auto-start (HKCU Run)
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "Zhongzhuan" /f
echo Zhongzhuan auto-start removed.
pause