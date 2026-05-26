@echo off
REM Detect video signal on DeckLink device
REM Usage: detect_signal.bat [device_index]
REM Example: detect_signal.bat 0   (CH01, device index 0)
REM Example: detect_signal.bat 1   (CH02, device index 1)

setlocal enabledelayexpansion

REM Default to device 1 (CH02)
set DEVICE_INDEX=1
if not "%~1"=="" set DEVICE_INDEX=%~1

echo.
echo ========================================================================
echo DeckLink Signal Detection
echo ========================================================================
echo Device Index: %DEVICE_INDEX%
echo.

"%~dp0..\venv\Scripts\python.exe" "%~dp0..\debugging\detect_decklink_signal.py" %DEVICE_INDEX%

echo.
pause
