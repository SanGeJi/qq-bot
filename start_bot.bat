@echo off
chcp 65001 >nul 2>&1
title QQ Bot

cd /d "%~dp0"

:: First run: no config.json -> enter setup wizard
if not exist "config.json" (
    echo ===============================================
    echo   First run detected - entering setup wizard...
    echo ===============================================
    echo.
    python "%~dp0setup.py"
    exit /b
)

:: Normal startup
if exist "launcher.py" (
    python "%~dp0launcher.py"
) else (
    python "%~dp0main.py"
)

pause