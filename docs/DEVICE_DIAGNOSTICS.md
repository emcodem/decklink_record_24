# DeckLink Device Diagnostics

Scripts live in `debugging\` within this project. The `.bat` wrappers are in `C:\dev\cmd_recorder\scripts\tools\`.

## Quick Status Check

To see which DeckLink devices are available and which are locked:

```powershell
# Via wrapper (recommended):
C:\dev\cmd_recorder\scripts\tools\check_devices.bat

# Or directly:
venv\Scripts\python.exe debugging\check_decklink_availability.py
```

Output will show:
```
[READY] These devices can be used:
  [0] DeckLink Quad (1)
  [1] DeckLink Quad (2)
  [2] DeckLink Quad (3)
  [3] DeckLink Quad (4)

[BLOCKED] These devices are in use:
  [4] DeckLink Quad (5) - in use by another app
  [5] DeckLink Quad (6) - in use by another app
  [6] DeckLink Quad (7) - in use by another app
  [7] DeckLink Quad (8) - in use by another app
```

## Understanding Device Indices

- **Device Index:** 0-based numbering from DeckLink enumeration
- **Channel Number:** 1-based numbering in cmd_recorder config (CH01, CH02, etc.)

### Mapping

| Device Index | Channel | Device Name |
|---|---|---|
| 0 | CH01 | DeckLink Quad (1) |
| 1 | CH02 | DeckLink Quad (2) |
| 2 | CH03 | DeckLink Quad (3) |
| 3 | CH04 | DeckLink Quad (4) |
| 4 | CH05 | DeckLink Quad (5) - LOCKED |
| 5 | CH06 | DeckLink Quad (6) - LOCKED |
| 6 | CH07 | DeckLink Quad (7) - LOCKED |
| 7 | CH08 | DeckLink Quad (8) - LOCKED |

## If You See "BLOCKED" Devices

This means other applications are using those devices. **Solutions:**

### 1. Close the Control Panel
```powershell
taskkill /IM BlackmagicControlPanel.exe /F
```

### 2. Close Media Express
```powershell
taskkill /IM "Media Express.exe" /F
```

### 3. Close DaVinci Resolve
```powershell
taskkill /IM DaVinciResolve.exe /F
```

### 4. Stop Other ffrecord Instances
```powershell
taskkill /IM python.exe /F
```
WARNING: This kills ALL Python processes.

### 5. If Still Blocked: Restart Windows
```powershell
shutdown /r /t 0
```

## Automatic Startup Check

When `C:\dev\cmd_recorder\launch_all.ps1` runs, it automatically:
1. Checks all 8 DeckLink devices
2. Reports which ones are ready
3. Reports which ones are locked
4. Shows the device names and indices

Example output:
```
[*] Checking DeckLink device availability...
    Ready: 4 device(s)
      [0] DeckLink Quad (1)
      [1] DeckLink Quad (2)
      ...
    Locked: 4 device(s)
      [4] DeckLink Quad (5)
      ...
    Run: C:\dev\cmd_recorder\scripts\tools\check_devices.bat
```

## Detailed Diagnostics

For complete device analysis including video format support:

```powershell
# Via wrapper (recommended):
C:\dev\cmd_recorder\scripts\tools\diagnose.bat

# Or directly:
venv\Scripts\python.exe debugging\diagnose_decklink.py
```

This will test:
- DLL loading
- COM module initialization
- Device enumeration
- IDeckLinkInput interface access
- Video format negotiation (1080i50)

## Troubleshooting Workflow

1. Run: `C:\dev\cmd_recorder\scripts\tools\check_devices.bat`
2. Check which devices show `[AVAILABLE]`
3. If locked, close competing applications
4. Re-run availability check
5. Once ready, run: `C:\dev\cmd_recorder\launch_all.ps1`
6. Monitor: `C:\temp\launch_log.txt`

## Key Files

- **`debugging\check_decklink_availability.py`** — Quick device status (READY/LOCKED)
- **`debugging\diagnose_decklink.py`** — Detailed device diagnostics
- **`C:\dev\cmd_recorder\scripts\tools\check_devices.bat`** — Wrapper for check_decklink_availability.py
- **`C:\dev\cmd_recorder\scripts\tools\diagnose.bat`** — Wrapper for diagnose_decklink.py
- **`C:\dev\cmd_recorder\src\record_ffrecord.py`** — Main recorder (runs startup check automatically)
- **`C:\dev\cmd_recorder\docs\MIGRATION_SUMMARY.md`** — Full deployment guide

## Notes

- The startup check is **non-blocking** — recording starts even if some devices are locked
- Only configured channels are needed (see `C:\dev\cmd_recorder\config.json` for enabled channels)
- Devices [4-7] can be left locked if not in use
- The system will retry failed devices with exponential backoff
