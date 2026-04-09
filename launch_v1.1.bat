@echo off
:: FFB Frequency Analyzer v1.1 – fallback launcher (no MOZA, no wheel button)
cd /d "%~dp0"
start "" /B C:\Python314\pythonw.exe ffb_analyzer_v1.1.py
