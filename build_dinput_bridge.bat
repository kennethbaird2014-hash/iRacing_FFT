@echo off
:: build_dinput_bridge.bat — Compile dinput_bridge.dll from dinput_bridge.cpp
::
:: This bridge uses pure Windows DirectInput (dinput8.dll) with EXCLUSIVE
:: cooperative level, bypassing MOZA Pit House routing entirely.
:: No MOZA SDK dependency required.
::
:: Prerequisites:
::   1. Visual Studio 2022 (Community is fine)
::   2. Run from "x64 Native Tools Command Prompt for VS 2022"
::      OR run first:
::      "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"
::
:: Output: dinput_bridge.dll  (place next to ffb_analyzer_v2.py)

setlocal

:: Verify cl.exe is available
where cl >nul 2>&1
if errorlevel 1 (
    echo ERROR: cl.exe not found.
    echo Run from a VS 2022 x64 Native Tools prompt, or run vcvars64.bat first.
    pause
    exit /b 1
)

echo Building dinput_bridge.dll ...
cl /nologo /LD /EHsc /O2 /MD /std:c++17 ^
   /DDIRECTINPUT_VERSION=0x0800 /DUNICODE /D_UNICODE ^
   dinput_bridge.cpp ^
   /link dinput8.lib dxguid.lib ^
   /out:dinput_bridge.dll

if errorlevel 1 (
    echo.
    echo BUILD FAILED.
    pause
    exit /b 1
)

echo.
echo SUCCESS: dinput_bridge.dll built.
echo Place dinput_bridge.dll next to ffb_analyzer_v2.py and run the app.
pause
