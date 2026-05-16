#!/usr/bin/env python3
"""
check_decklink_in_use.py — Find processes that may be holding DeckLink devices open

Runs `tasklist` and filters output for known DeckLink consumers:
  Blackmagic Desktop Video, Media Express, Fusion, DaVinci Resolve,
  Premiere, ffrecord, ffmpeg, python

Use this when EnableVideoInput returns E_FAIL (device locked) to quickly
identify which application is holding the device.  No COM calls are made,
so this script is safe to run even when the DeckLink driver is in a bad state.

Usage:
    python check_decklink_in_use.py
"""

import subprocess
import sys


def check_decklink_usage():
    print("=" * 70)
    print("DeckLink Device Usage Check")
    print("=" * 70)

    decklink_processes = [
        "Blackmagic Desktop Video",
        "Media Express",
        "Fusion",
        "DaVinci Resolve",
        "Premiere",
        "ffrecord",
        "ffmpeg",
        "python",
    ]

    print("\nLooking for DeckLink-related processes...")
    print("-" * 70)

    try:
        result = subprocess.run(
            ["tasklist"],
            capture_output=True,
            text=True,
            timeout=5,
        )

        found_any = False
        for line in result.stdout.split("\n"):
            for proc_name in decklink_processes:
                if proc_name.lower() in line.lower():
                    print(f"[FOUND] {line.strip()}")
                    found_any = True
                    break

        if not found_any:
            print("No obvious DeckLink-related processes found")

    except Exception as e:
        print(f"Error checking processes: {e}")

    print("\n" + "=" * 70)
    print("COMMON FIXES for 'Device In Use' Error:")
    print("=" * 70)
    print("1. Close Blackmagic Desktop Video Control Panel")
    print("2. Close Media Express or any DaVinci Resolve instance")
    print("3. Stop any other ffrecord processes:")
    print("   taskkill /IM python.exe /F  (use with caution!)")
    print("4. Wait a few seconds and try again")
    print("5. If still failing, restart the computer")
    print("=" * 70)


if __name__ == "__main__":
    check_decklink_usage()
