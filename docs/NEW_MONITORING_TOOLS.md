# Enhanced Logging & Monitoring Tools - Complete Summary

Python scripts live in `debugging\` within this project (`decklink_record_24`).
The `.bat` wrappers that invoke them are in `C:\dev\cmd_recorder\scripts\tools\`.

## Problem Statement

The CH02 recorder was running with **0 fps for extended periods with NO ERROR MESSAGES** in logs. The device initialized successfully but received no frames, creating a "silent failure" that was impossible to diagnose without external tools.

## Solution: Enhanced Monitoring

Added **four new tools** to catch and diagnose silent failures automatically.

---

## New Tools Created

### 1. **Real-Time Capture Health Monitor**

**Files:**
- `debugging\monitor_capture_health.py` (Python script)
- `C:\dev\cmd_recorder\scripts\tools\monitor_capture_health.bat` (wrapper)

**Purpose:** Watch logs in real-time and alert when device initializes but 0 fps persists.

**Usage:**
```powershell
C:\dev\cmd_recorder\scripts\tools\monitor_capture_health.bat          # Default: 30 second threshold
C:\dev\cmd_recorder\scripts\tools\monitor_capture_health.bat 60       # Custom: 60 second threshold
```

**What it does:**
- Continuously reads log files from `C:\dev\cmd_recorder\logs\chXX\` directories
- Detects pattern: "DeckLink capture started" followed by 0.0 fps
- After threshold seconds (default 30), prints alert with:
  - **Top 3 likely causes:** No signal, format mismatch, device locked
  - **Specific instructions** to check each cause
  - **Recommended diagnostic commands** to run next

**Alert example:**
```
[ALERT] 14:38:20 - ch02 has 0 fps for 30+ seconds

SILENT FAILURE DETECTED: Device initialized but not capturing frames

LIKELY CAUSES (check in this order):
1. NO VIDEO SIGNAL on input
2. FORMAT MISMATCH
3. DEVICE IN USE by another application
```

**Recommended usage:**
```
Terminal 1: C:\dev\cmd_recorder\launch_all.ps1
Terminal 2: C:\dev\cmd_recorder\scripts\tools\monitor_capture_health.bat
```

---

### 2. **Active Signal Detection**

**Files:**
- `debugging\detect_decklink_signal.py` (Python script)
- `C:\dev\cmd_recorder\scripts\tools\detect_signal.bat` (wrapper)

**Purpose:** Actively detect whether video signal is reaching the device.

**Usage:**
```powershell
C:\dev\cmd_recorder\scripts\tools\detect_signal.bat 0      # Check CH01 (device index 0)
C:\dev\cmd_recorder\scripts\tools\detect_signal.bat 1      # Check CH02 (device index 1)
```

**What it does:**
- Attempts to enable video input on the specified device
- Tests all common formats: 1080i50, 1080p50, 720p50, 4K formats, etc.
- Reports which format (if any) has signal
- Distinguishes between:
  - Signal present and format detected
  - No signal on device
  - Device locked by another app

**Output when signal detected:**
```
[OK] Format DETECTED: 1080i50
SIGNAL DETECTED: 1080i50
```

**Output when NO signal:**
```
[FAIL] 1080i50 -- not available
[FAIL] 1080p50 -- not available
...all formats fail...
NO SIGNAL DETECTED on device
```

**Next steps after detection:**
- If format detected: verify it matches YAML config in `C:\dev\cmd_recorder\ffrecord_configs\`
- If no signal: check cable and video source

---

### 3. **Automated Full Troubleshooting**

**File:** `C:\dev\cmd_recorder\scripts\tools\quick_troubleshoot.bat`

**Purpose:** Run all diagnostics automatically in the right order.

**Usage:**
```powershell
C:\dev\cmd_recorder\scripts\tools\quick_troubleshoot.bat
```

**What it does:**
1. Checks device availability (is it locked?)
2. Checks signal on CH01 and CH02
3. Checks running processes (Blackmagic, ffrecord, etc.)
4. Shows last 30 lines of logs for both channels
5. Provides analysis summary with recommended fixes

**Time to run:** ~3 minutes (fully automated)

**Output:** Complete diagnosis with step-by-step fix instructions

---

### 4. **Quick Reference Card**

**File:** `C:\dev\cmd_recorder\MONITORING_QUICKREF.txt`

**Purpose:** Portable quick-reference for common issues and commands.

---

## Integration with Existing Tools

| Tool | Location | Purpose | Time | When |
|------|----------|---------|------|------|
| `check_decklink_availability.py` | `debugging\` | Device locked check | 10s | Device appears unavailable |
| `detect_signal.bat` | `cmd_recorder\scripts\tools\` | Signal presence check | 15s | No frames captured |
| `monitor_capture_health.bat` | `cmd_recorder\scripts\tools\` | Real-time monitoring | Continuous | While recording |
| `quick_troubleshoot.bat` | `cmd_recorder\scripts\tools\` | Full automated check | 3m | When confused about the issue |
| `diagnose_decklink.py` | `debugging\` | Detailed device info | 1m | Advanced troubleshooting |

---

## How to Use: Step-by-Step

### For Normal Recording:
```powershell
# Terminal 1: Start recording
C:\dev\cmd_recorder\launch_all.ps1

# Terminal 2: Monitor for issues (optional but recommended)
C:\dev\cmd_recorder\scripts\tools\monitor_capture_health.bat
```

### When Something Goes Wrong:
```powershell
# Run this first (automated, finds the issue)
C:\dev\cmd_recorder\scripts\tools\quick_troubleshoot.bat

# If issue is unclear, run specific tools:
C:\dev\cmd_recorder\scripts\tools\check_devices.bat            # Device locked?
C:\dev\cmd_recorder\scripts\tools\detect_signal.bat 1          # Signal present?
C:\dev\cmd_recorder\scripts\tools\monitor_capture_health.bat   # Watch logs live
```

### When Debugging Signal Issues:
```powershell
# Check CH02 signal
C:\dev\cmd_recorder\scripts\tools\detect_signal.bat 1

# If signal detected but wrong format, update config:
# Edit: C:\dev\cmd_recorder\ffrecord_configs\ch02.yaml
#   expected_format: "1080i50"

# Restart recorder
C:\dev\cmd_recorder\launch_all.ps1
```

---

## Real-World Usage Example

**Scenario:** CH02 shows 0 fps in logs. What do you do?

```powershell
# Step 1: Quick automatic diagnosis (3 minutes)
C:\dev\cmd_recorder\scripts\tools\quick_troubleshoot.bat

# Output shows:
# - Device [AVAILABLE] (not locked)
# - Signal: NO SIGNAL on CH02

# Step 2: Follow the recommendation
# - Check SDI cable is plugged into device
# - Verify video source is powered on and outputting

# Step 3: Fix the issue (reconnect cable)

# Step 4: Restart recording
C:\dev\cmd_recorder\launch_all.ps1

# Step 5: Optional - monitor to confirm it works
C:\dev\cmd_recorder\scripts\tools\monitor_capture_health.bat
# Should show: "[14:45:30] ch02 RECOVERED - 25.0 fps"
```

---

## Files

### In this project (`decklink_record_24`):
```
debugging\monitor_capture_health.py      <- Real-time log monitor (main logic)
debugging\detect_decklink_signal.py      <- Signal detection script (main logic)
debugging\check_decklink_availability.py <- Device availability check
debugging\check_decklink_in_use.py       <- Process usage check
debugging\diagnose_decklink.py           <- Detailed device diagnostics
```

### In cmd_recorder:
```
scripts\tools\monitor_capture_health.bat <- Wrapper for monitor_capture_health.py
scripts\tools\detect_signal.bat          <- Wrapper for detect_decklink_signal.py
scripts\tools\check_devices.bat          <- Wrapper for check_decklink_availability.py
scripts\tools\diagnose.bat               <- Wrapper for diagnose_decklink.py
scripts\tools\quick_troubleshoot.bat     <- Automated full diagnostic
ffrecord_configs\ch01.yaml               <- Channel 1 config
ffrecord_configs\ch02.yaml               <- Channel 2 config
logs\ch01\, logs\ch02\                   <- Per-channel log directories
```
