@echo off
:: FFB Frequency Analyzer – first-time setup
:: Run this ONCE before using launch.bat
:: Requires Python 3.10+ installed with "Add Python to PATH" checked

echo ============================================
echo  FFB Frequency Analyzer -- Install
echo ============================================
echo.

python --version 2>nul
if errorlevel 1 (
    echo ERROR: Python not found on PATH.
    echo Download from https://python.org -- tick "Add Python to PATH".
    pause
    exit /b 1
)

echo Installing / updating dependencies...
echo.
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo.
echo ============================================
echo  Done!  Run launch.bat to start the app.
echo ============================================
pause
