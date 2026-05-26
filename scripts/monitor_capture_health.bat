@echo off
REM Monitor ffrecord capture health in real-time
REM Usage: monitor_capture_health.bat [threshold_seconds]
REM Example: monitor_capture_health.bat 30

setlocal enabledelayexpansion

REM Default to 30 seconds threshold
set THRESHOLD=30
if not "%~1"=="" set THRESHOLD=%~1

echo.
echo ========================================================================
echo FFRecord Capture Health Monitor
echo ========================================================================
echo This tool detects silent failures: device initialized but 0 fps
echo.
echo Threshold: %THRESHOLD% seconds of 0 fps before alert
echo.
echo Press Ctrl+C to stop monitoring
echo ========================================================================
echo.

"%~dp0..\venv\Scripts\python.exe" "%~dp0..\debugging\monitor_capture_health.py" C:\dev\cmd_recorder\logs %THRESHOLD%

pause
