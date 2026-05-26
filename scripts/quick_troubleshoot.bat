@echo off
REM Quick troubleshooting script - runs all diagnostics in order

setlocal enabledelayexpansion

color 0F
cls

echo.
echo ================================================================================
echo  FFRecord Quick Troubleshoot
echo ================================================================================
echo.
echo This script will run diagnostics to identify your capture issue.
echo Press Ctrl+C anytime to stop.
echo.
pause

REM Step 1: Device Availability
echo.
echo ================================================================================
echo  STEP 1: Checking device availability (is device locked?)
echo ================================================================================
echo.
"%~dp0..\venv\Scripts\python.exe" "%~dp0..\debugging\check_decklink_availability.py"

echo.
echo Press any key to continue to Step 2...
pause >nul

REM Step 2: Signal Detection
echo.
echo ================================================================================
echo  STEP 2: Detecting video signal (is signal reaching device?)
echo ================================================================================
echo.
echo Checking CH01 (device index 0)...
"%~dp0..\venv\Scripts\python.exe" "%~dp0..\debugging\detect_decklink_signal.py" 0

echo.
echo Checking CH02 (device index 1)...
"%~dp0..\venv\Scripts\python.exe" "%~dp0..\debugging\detect_decklink_signal.py" 1

echo.
echo Press any key to continue to Step 3...
pause >nul

REM Step 3: Process Check
echo.
echo ================================================================================
echo  STEP 3: Checking running processes
echo ================================================================================
echo.
echo Looking for ffrecord and other recording processes:
tasklist | find /I "python" && (echo. & echo [!] Python process found) || echo [OK] No other Python processes

echo.
echo Blackmagic-related processes:
tasklist | find /I "Blackmagic" && (echo. & echo [ALERT] Blackmagic control panel is running) || echo [OK] No Blackmagic processes

echo.
echo Press any key to continue to Step 4...
pause >nul

REM Step 4: Log Analysis
echo.
echo ================================================================================
echo  STEP 4: Recent logs
echo ================================================================================
echo.
echo Last 30 lines from CH01:
if exist C:\dev\cmd_recorder\logs\ch01\*.log (
    powershell -Command "Get-ChildItem C:\dev\cmd_recorder\logs\ch01\*.log | Sort-Object LastWriteTime -Descending | Select-Object -First 1 | ForEach-Object { Get-Content $_.FullName -Tail 30 }"
) else (
    echo [!] No logs found for CH01
)

echo.
echo Last 30 lines from CH02:
if exist C:\dev\cmd_recorder\logs\ch02\*.log (
    powershell -Command "Get-ChildItem C:\dev\cmd_recorder\logs\ch02\*.log | Sort-Object LastWriteTime -Descending | Select-Object -First 1 | ForEach-Object { Get-Content $_.FullName -Tail 30 }"
) else (
    echo [!] No logs found for CH02
)

echo.
echo ================================================================================
echo  ANALYSIS COMPLETE
echo ================================================================================
echo.
echo Summary of findings:
echo.
echo IF device shows [IN USE]:
echo   1. Close Blackmagic Desktop Video Control Panel
echo   2. Close DaVinci Resolve, Media Express, OBS, or other apps
echo   3. Re-run this script to verify [AVAILABLE]
echo.
echo IF signal detection shows [SIGNAL DETECTED]:
echo   - Check if format matches your config
echo   - If different format, update C:\dev\cmd_recorder\ffrecord_configs\chXX.yaml
echo.
echo IF signal detection shows [NO SIGNAL]:
echo   - Check SDI/HDMI cable connection
echo   - Verify video source is powered on and actively outputting
echo   - Try disconnecting/reconnecting the cable
echo.
echo IF 0 fps in logs:
echo   - Could be: no signal, format mismatch, or device locked
echo   - Use: monitor_capture_health.bat to watch in real-time
echo.
echo ================================================================================
echo.
pause
