#!/usr/bin/env python3
"""
check_decklink_availability.py — Test each DeckLink device for availability

For every device found by the COM iterator, attempts EnableVideoInput with
1080i50.  Classifies the result as:
  [AVAILABLE]    — device accepted the call; can be used for recording
  [IN USE]       — returned E_FAIL / E_ACCESSDENIED; likely locked by another app
  [INACCESSIBLE] — IDeckLinkInput interface could not be obtained at all

Prints a summary with remediation steps for locked devices.

Usage:
    python check_decklink_availability.py
"""

import sys
from pathlib import Path

# Make ffrecord importable (parent of this file is decklink_record_24/)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def check_device_availability():
    print("=" * 70)
    print("DeckLink Device Availability Check")
    print("=" * 70)

    try:
        from comtypes.client import GetModule, CreateObject
    except ImportError as e:
        print(f"[ERROR] Failed to import comtypes: {e}")
        return

    dll_path = r"C:\Program Files\Blackmagic Design\Blackmagic Desktop Video\DeckLinkAPI64.dll"

    print(f"\n[1] Loading DeckLink DLL...")
    try:
        decklink_module = GetModule(dll_path)
        print("    [OK] DLL loaded")
    except Exception as e:
        print(f"    [ERROR] Failed: {e}")
        return

    print(f"\n[2] Creating iterator...")
    try:
        iterator = CreateObject(decklink_module.CDeckLinkIterator)
        print("    [OK] Iterator created")
    except Exception as e:
        print(f"    [ERROR] Failed: {e}")
        return

    print(f"\n[3] Testing each device for availability...\n")
    print("-" * 70)

    devices = []
    idx = 0
    while True:
        try:
            device = iterator.Next()
            if device is None:
                break
            name = device.GetDisplayName()
            devices.append((idx, device, name))
            idx += 1
        except Exception:
            break

    if not devices:
        print("No devices found")
        return

    available_devices = []
    locked_devices = []

    for idx, device, name in devices:
        try:
            decklink_input = device.QueryInterface(decklink_module.IDeckLinkInput)

            try:
                hr = decklink_input.EnableVideoInput(
                    decklink_module.bmdModeHD1080i50,
                    decklink_module.bmdFormat8BitYUV,
                    decklink_module.bmdVideoInputEnableFormatDetection,
                )
                if hr == 0:
                    print(f"[{idx}] {name:30} [AVAILABLE]")
                    available_devices.append((idx, name))
                else:
                    if hr == 0x80004005:  # E_FAIL
                        print(f"[{idx}] {name:30} [IN USE - E_FAIL]")
                        locked_devices.append((idx, name, "E_FAIL (likely in use)"))
                    elif hr == 0x80070005:  # E_ACCESSDENIED
                        print(f"[{idx}] {name:30} [IN USE - ACCESS DENIED]")
                        locked_devices.append((idx, name, "Access denied"))
                    else:
                        print(f"[{idx}] {name:30} [ERROR - hr={hr:#010x}]")
                        locked_devices.append((idx, name, f"Error {hr:#010x}"))
            except Exception as e:
                if "already" in str(e).lower() or "use" in str(e).lower():
                    print(f"[{idx}] {name:30} [IN USE]")
                    locked_devices.append((idx, name, "Exception (likely in use)"))
                else:
                    print(f"[{idx}] {name:30} [ERROR]")
                    locked_devices.append((idx, name, str(e)[:50]))

        except Exception:
            print(f"[{idx}] {name:30} [INACCESSIBLE]")
            locked_devices.append((idx, name, "Cannot query interface"))

    print("-" * 70)
    print("\n[SUMMARY]\n")
    print(f"Total devices found: {len(devices)}")
    print(f"Available (can record): {len(available_devices)}")
    print(f"In use / Locked: {len(locked_devices)}")

    if available_devices:
        print(f"\n[READY] These devices can be used:")
        for idx, name in available_devices:
            print(f"  [{idx}] {name}")

    if locked_devices:
        print(f"\n[BLOCKED] These devices are in use:")
        for idx, name, reason in locked_devices:
            print(f"  [{idx}] {name} - {reason}")

        print(f"\n[TO FIX] Close applications using these devices:")
        print(f"  1. Close Blackmagic Desktop Video Control Panel")
        print(f"  2. Close Media Express")
        print(f"  3. Close DaVinci Resolve")
        print(f"  4. Close any other ffrecord instances")
        print(f"  5. If still locked, restart Windows")

    print("\n" + "=" * 70)


if __name__ == "__main__":
    check_device_availability()
