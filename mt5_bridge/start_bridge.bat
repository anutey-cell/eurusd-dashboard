@echo off
title MT5 Price Bridge — Port 8765
echo =========================================
echo  MT5 Windows Price Bridge
echo  Serves live Exness prices to Docker
echo  Port: 8765
echo =========================================
echo.

:: Install deps if not already installed
pip show MetaTrader5 >nul 2>&1
if errorlevel 1 (
    echo Installing dependencies...
    pip install -r "%~dp0requirements.txt"
)

echo Starting bridge...
echo Docker backend will connect via http://host.docker.internal:8765
echo Press Ctrl+C to stop.
echo.

python "%~dp0mt5_bridge.py"
pause
