@echo off
title ShareVideo DLNA Media Server
cd /d "%~dp0"

echo.
echo ============================================
echo   ShareVideo DLNA Media Server
echo ============================================
echo.
echo   Checking dependencies...
echo.

pip install -r requirements.txt >nul 2>&1

echo   Starting server...
echo.

python server.py %*

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo ============================================
    echo   Server exited with error code %ERRORLEVEL%
    echo ============================================
    pause
)
