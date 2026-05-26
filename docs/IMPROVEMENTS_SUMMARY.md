# Device Detection Improvements - Complete

Scripts in this document live in `debugging\` within this project (`decklink_record_24`).
The `.bat` wrappers that call them are in `C:\dev\cmd_recorder\scripts\tools\`.

## What Was Added

### 1. Smart Device Availability Detection

**`debugging\check_decklink_availability.py`** — Actually tests each device

- Enumerates all 8 DeckLink devices
- **Tests each one** to see if it's available or locked
- Shows clear `[AVAILABLE]` vs `[IN USE]` status
- Provides actionable fix instructions
- Replaces unreliable process name searching

Example output:
```
[0] DeckLink Quad (1)    [AVAILABLE]
[1] DeckLink Quad (2)    [AVAILABLE]
[2] DeckLink Quad (3)    [AVAILABLE]
[3] DeckLink Quad (4)    [AVAILABLE]
[4] DeckLink Quad (5)    [IN USE]
[5] DeckLink Quad (6)    [IN USE]
```

### 2. Automatic Startup Check

**Updated `C:\dev\cmd_recorder\src\record_ffrecord.py`** — Runs device check at launch

- Automatically detects device status on startup
- Shows ready vs locked devices
- Informs user which devices are working
- Non-blocking (recording starts even if some locked)

Startup output:
```
[*] Checking DeckLink device availability...
    Ready: 4 device(s)
      [0] DeckLink Quad (1)
      [1] DeckLink Quad (2)
      [2] DeckLink Quad (3)
      [3] DeckLink Quad (4)
    Locked: 4 device(s)
      [4] DeckLink Quad (5)
      [5] DeckLink Quad (6)
      [6] DeckLink Quad (7)
      [7] DeckLink Quad (8)
    Run: C:\dev\cmd_recorder\scripts\tools\check_devices.bat
```

### 3. Enhanced Error Messages

**Improved `C:\dev\cmd_recorder\src\record_ffrecord.py`** — Better error detection

- Detects "device in use" errors specifically
- Distinguishes between:
  - Device locked by another app (E_FAIL)
  - No video signal (timeout/no response)
  - Format mismatch (device outputting different format)
- Shows up to 3 failures before directing to diagnostics

Error output:
```
[ATTENTION] Process failed 3 times. Common causes:
  1. Device already open by another application
     -> Close Blackmagic Desktop Video Control Panel
     -> Close other ffrecord instances
  2. No video signal on SDI input
     -> Verify SDI cable is connected
     -> Verify video source is active
  3. Device outputting different format than configured
     -> Check Blackmagic control panel
     -> Update YAML config if needed
```

### 4. Improved Diagnostics

**Enhanced `debugging\diagnose_decklink.py`** — Clearer error messages

- Shows specific E_FAIL error handling
- Lists 3 likely causes with solutions
- Suggests running availability checker as next step

Output on E_FAIL:
```
[ERROR] Format check failed: (-2147467259, 'Unbekannter Fehler'...)
LIKELY CAUSES (E_FAIL / Device In Use):
1. Device is ALREADY OPEN by another application
   -> Close Blackmagic Desktop Video Control Panel
   -> Close any other ffrecord instances
   -> Run: C:\dev\cmd_recorder\scripts\tools\check_devices.bat
2. No video signal connected to device input
   -> Verify SDI cable is connected to device
   -> Verify video source is powered and outputting
3. Device format mismatch
   -> Check Blackmagic control panel for signal format
   -> Update YAML config if needed
```

## User Experience Flow

### Normal Path
```
1. C:\dev\cmd_recorder\scripts\tools\check_devices.bat
   -> Shows [READY] devices and [LOCKED] devices
2. C:\dev\cmd_recorder\launch_all.ps1
   -> Automatic startup check confirms devices
   -> Shows which are ready, which are locked
   -> Starts recording on available devices
3. Recording begins automatically
```

### Troubleshooting Path
```
1. Device shows [LOCKED]
2. Run: C:\dev\cmd_recorder\scripts\tools\check_devices.bat
3. See which app is locking it
4. Close that app
5. Re-run availability check
6. Confirm [AVAILABLE]
7. Start recording
```

### Failed Recording Path
```
1. Recording fails 3 times
2. record_ffrecord.py shows error message
3. User sees "Device already open by another application" message
4. Knows to run: C:\dev\cmd_recorder\scripts\tools\check_devices.bat
5. Follows fix instructions
```

## Key Improvements

| Old Approach | New Approach |
|---|---|
| Generic "E_FAIL" error | "Device already open" + solutions |
| `check_decklink_in_use.py` searches for process names | `check_decklink_availability.py` actually tests devices |
| No startup diagnostics | Automatic device check at launch |
| Manual troubleshooting | Directed error messages with fixes |
| Silent failures | 3-strike warning with guidance |

## Testing Results

Device detection working correctly:
- 4 devices available (indices 0-3)
- 4 devices locked (indices 4-7)
- Startup check displays both
- User can now see exactly which devices are ready

## Files

### In this project (`decklink_record_24`):
```
debugging\check_decklink_availability.py   <- Device availability check (READY/LOCKED)
debugging\check_decklink_in_use.py         <- Process usage check
debugging\diagnose_decklink.py             <- Detailed device diagnostics
```

### In cmd_recorder:
```
C:\dev\cmd_recorder\scripts\tools\check_devices.bat   <- Wrapper for check_decklink_availability.py
C:\dev\cmd_recorder\scripts\tools\diagnose.bat         <- Wrapper for diagnose_decklink.py
C:\dev\cmd_recorder\src\record_ffrecord.py             <- Main recorder (runs startup check)
C:\dev\cmd_recorder\docs\DEVICE_DIAGNOSTICS.md         <- Channel index mapping & diagnostics guide
C:\dev\cmd_recorder\docs\MIGRATION_SUMMARY.md          <- Full deployment guide
```
