#!/usr/bin/env python3
"""
diagnose_decklink.py — DeckLink COM initialization diagnostic

Step-by-step test of the full DeckLink COM stack:
  1. Verifies the DeckLinkAPI64.dll is present
  2. Loads the type library via comtypes
  3. Creates a CDeckLinkIterator and enumerates devices
  4. For each device, calls EnableVideoInput to check availability

Run this first when ffrecord fails to open a device (E_FAIL / 0x80004005).
It distinguishes between: DLL missing, no devices, device locked, and
format mismatch.

Usage:
    python diagnose_decklink.py
"""

import sys
import os
from pathlib import Path

# Make ffrecord importable (parent of this file is decklink_record_24/)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def diagnose():
    print("=" * 70)
    print("DeckLink Diagnostic Tool")
    print("=" * 70)

    # Step 1: Check DLL
    dll_path = r"C:\Program Files\Blackmagic Design\Blackmagic Desktop Video\DeckLinkAPI64.dll"
    print(f"\n[1] DeckLink DLL: {dll_path}")
    if os.path.exists(dll_path):
        print("    [OK] DLL found")
    else:
        print("    [ERROR] DLL not found")
        return

    # Step 2: Load module
    print("\n[2] Loading DeckLink type library with comtypes...")
    try:
        from comtypes.client import GetModule
        decklink_module = GetModule(dll_path)
        print("    [OK] Module loaded")
    except Exception as e:
        print(f"    [ERROR] Failed: {e}")
        return

    # Step 3: Create iterator
    print("\n[3] Creating DeckLink iterator...")
    try:
        from comtypes.client import CreateObject
        iterator = CreateObject(decklink_module.CDeckLinkIterator)
        print("    [OK] Iterator created")
    except Exception as e:
        print(f"    [ERROR] Failed: {e}")
        return

    # Step 4: Enumerate devices
    print("\n[4] Enumerating devices...")
    devices = []
    idx = 0
    try:
        while True:
            try:
                device = iterator.Next()
                if device is None:
                    break
                name = device.GetDisplayName()
                devices.append((idx, device, name))
                print(f"    [{idx}] {name}")
                idx += 1
            except Exception as e:
                print(f"    Error enumerating device {idx}: {e}")
                break
    except Exception as e:
        print(f"    Error during enumeration: {e}")

    if not devices:
        print("    [ERROR] No devices found")
        return

    # Step 5: Try to access input interface
    print("\n[5] Testing device interfaces...")
    for idx, device, name in devices[:2]:  # Test first 2 devices
        print(f"\n    Device [{idx}]: {name}")
        try:
            decklink_input = device.QueryInterface(decklink_module.IDeckLinkInput)
            print(f"      [OK] IDeckLinkInput interface obtained")

            print(f"      Checking video format support...")
            try:
                hr = decklink_input.EnableVideoInput(
                    decklink_module.bmdModeHD1080i50,
                    decklink_module.bmdFormat8BitYUV,
                    decklink_module.bmdVideoInputEnableFormatDetection,
                )
                if hr == 0:
                    print(f"      [OK] 1080i50 format supported (EnableVideoInput returned 0)")
                else:
                    if hr == 0x80004005:  # E_FAIL
                        print(f"      [ERROR] EnableVideoInput failed with E_FAIL (0x{hr:08x})")
                        print(f"      LIKELY CAUSES:")
                        print(f"      1. Device is ALREADY OPEN by another application")
                        print(f"         -> Close Blackmagic Desktop Video Control Panel")
                        print(f"         -> Close any other ffrecord instances")
                        print(f"      2. No video signal connected to device input")
                        print(f"         -> Verify SDI cable is connected")
                        print(f"         -> Verify video source is outputting signal")
                        print(f"      3. Device format mismatch")
                        print(f"         -> Check Blackmagic control panel for signal format")
                    elif hr == 0x80070005:  # E_ACCESSDENIED
                        print(f"      [ERROR] Access denied (0x{hr:08x})")
                        print(f"      -> Device is already open by another application")
                    else:
                        print(f"      [ERROR] EnableVideoInput failed (hr=0x{hr:08x})")
            except Exception as e:
                err_str = str(e)
                print(f"      [ERROR] Format check failed: {err_str}")

                if "-2147467259" in err_str or "0x80004005" in err_str or "E_FAIL" in err_str:
                    print(f"      LIKELY CAUSES (E_FAIL / Device In Use):")
                    print(f"      1. Device is ALREADY OPEN by another application")
                    print(f"         -> Close Blackmagic Desktop Video Control Panel")
                    print(f"         -> Close any other ffrecord instances")
                    print(f"         -> Run: python debugging\\check_decklink_in_use.py")
                    print(f"      2. No video signal connected to device input")
                    print(f"         -> Verify SDI cable is connected to device")
                    print(f"         -> Verify video source is powered and outputting")
                    print(f"      3. Device format mismatch")
                    print(f"         -> Check Blackmagic control panel for signal format")
                    print(f"         -> Update ffrecord YAML config if needed")

        except Exception as e:
            print(f"      [ERROR] Could not get IDeckLinkInput: {e}")

    print("\n" + "=" * 70)
    print("Diagnostic Summary:")
    print("=" * 70)
    print(f"Found {len(devices)} DeckLink device(s)")
    print("\nNext Steps:")
    print("1. Verify SDI cable is connected to the DeckLink input port")
    print("2. Verify video source is outputting 1080i50 (or adjust YAML config)")
    print("3. Check Blackmagic Desktop Video Control Panel for device status")
    print("4. Try disconnecting/reconnecting the SDI cable")
    print("5. Check if Windows Device Manager shows the DeckLink card")
    print("=" * 70)


if __name__ == "__main__":
    diagnose()
