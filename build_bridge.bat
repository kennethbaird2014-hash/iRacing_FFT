@echo off
:: build_bridge.bat — Compile moza_bridge.dll from moza_bridge.cpp
::
:: Prerequisites:
::   1. Visual Studio 2022 (Community edition is fine)
::   2. Run this from a "x64 Native Tools Command Prompt for VS 2022"
::      OR run:  "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"
::      before executing this script.
::
:: Output: moza_bridge.dll  (place next to ffb_analyzer_v2.py)

setlocal

set SDK_DIR=%~dp0MOZA_SDK\1.0.1.8\MSVC2022-64

:: Verify SDK exists
if not exist "%SDK_DIR%\lib\MOZA_SDK.lib" (
    echo ERROR: MOZA_SDK.lib not found at %SDK_DIR%\lib\
    echo Make sure the MOZA SDK is in the expected location.
    pause
    exit /b 1
)

:: Verify cl.exe is available
where cl >nul 2>&1
if errorlevel 1 (
    echo ERROR: cl.exe not found. Run this from a VS 2022 x64 Native Tools prompt.
    echo Or run: "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"
    pause
    exit /b 1
)

echo Building moza_bridge.dll ...
cl /nologo /LD /EHsc /O2 /MD /std:c++17 ^
   /DDIRECTINPUT_VERSION=0x0800 ^
   /I"%SDK_DIR%\include" ^
   moza_bridge.cpp ^
   /link /LIBPATH:"%SDK_DIR%\lib" MOZA_SDK.lib user32.lib ^
   /out:moza_bridge.dll

if errorlevel 1 (
    echo.
    echo BUILD FAILED.
    pause
    exit /b 1
)

:: Copy the MOZA SDK runtime DLL next to our bridge if not already there
if not exist "%~dp0MOZA_SDK.dll" (
    copy "%SDK_DIR%\bin\MOZA_SDK.dll" "%~dp0" >nul
    echo Copied MOZA_SDK.dll to project directory.
)

echo.
echo SUCCESS: moza_bridge.dll built.
echo Make sure MOZA_SDK.dll is in the same folder as moza_bridge.dll.
pause
