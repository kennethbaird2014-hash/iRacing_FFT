@echo off
:: FFB Frequency Analyzer v2.0 – one-click launcher
:: iRacing does NOT need to be running first.
:: The app waits and auto-connects when you enter a session.
cd /d "%~dp0"
start "" /B C:\Python314\pythonw.exe ffb_analyzer.py
