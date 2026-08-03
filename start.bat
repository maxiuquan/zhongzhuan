@echo off
title Zhongzhuan Proxy
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python -m zhongzhuan
pause
