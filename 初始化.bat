@echo off
chcp 65001 >nul 2>&1
title QQ Bot Setup

cd /d "%~dp0"

echo ===============================================
echo   QQ Bot - Configuration Setup
echo ===============================================
echo.
python "%~dp0setup.py"
pause