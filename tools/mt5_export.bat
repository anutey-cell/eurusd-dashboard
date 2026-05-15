@echo off
REM ============================================================================
REM MT5 XAU/USD Historical Exporter — Windows launcher
REM ============================================================================
REM Requires:
REM   1. MetaTrader 5 terminal installed and logged in
REM   2. Python 3.10+ on PATH
REM   3. MetaTrader5 Python package:  pip install MetaTrader5
REM
REM Defaults: H4 timeframe, 5 years history
REM Override with command-line args, e.g.:
REM   mt5_export.bat --timeframe M15 --years 2
REM ============================================================================

setlocal
cd /d "%~dp0"

echo.
echo === MT5 XAU/USD Historical Export ===
echo.
echo Checking Python install...
python --version 2>nul
if errorlevel 1 (
    echo ERROR: Python not found on PATH. Install Python 3.10+ first.
    pause
    exit /b 1
)

echo Checking MetaTrader5 package...
python -c "import MetaTrader5" 2>nul
if errorlevel 1 (
    echo MetaTrader5 package not installed. Installing now...
    pip install MetaTrader5
    if errorlevel 1 (
        echo ERROR: Failed to install MetaTrader5 package.
        pause
        exit /b 1
    )
)

echo.
echo Starting export...
echo.
python mt5_export.py %*

echo.
echo Done. Press any key to close.
pause >nul
