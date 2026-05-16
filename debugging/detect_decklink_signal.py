#!/usr/bin/env python3
"""
detect_decklink_signal.py — Probe a DeckLink device for incoming video signal

Iterates through the most common HD/4K formats and calls EnableVideoInput for
each.  A return code of 0 means the device accepted that format (signal
present and unlocked).  Reports the first matching format and gives copy-paste
instructions for updating the ffrecord YAML config.

Useful for distinguishing:
  • "no signal at all"  — EnableVideoInput fails for every format
  • "wrong format"      — succeeds only for a format that differs from the YAML
  • "device locked"     — all calls raise "already in use"

Usage:
    python detect_decklink_signal.py [device_index]

    device_index  0-based device index (default: 0 = first device / CH01)
"""

import sys
from pathlib import Path

# Make ffrecord importable (parent of this file is decklink_record_24/)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# Known ffrecord config directory (in the cmd_recorder sibling project)
_FFRECORD_CONFIGS = Path(__file__).resolve().parent.parent.parent / "cmd_recorder" / "ffrecord_configs"


def detect_signal(device_index=0):
    """
    Attempt to detect video signal on a DeckLink device.

    Returns: (signal_detected, format_detected, error_message)
    """
    print("=" * 70)
    print(f"Signal Detection for Device {device_index}")
    print("=" * 70)

    try:
        from comtypes.client import GetModule, CreateObject
    except ImportError as e:
        print(f"[ERROR] Failed to import comtypes: {e}")
        return False, None, "comtypes import failed"

    dll_path = r"C:\Program Files\Blackmagic Design\Blackmagic Desktop Video\DeckLinkAPI64.dll"

    try:
        decklink_module = GetModule(dll_path)
        iterator = CreateObject(decklink_module.CDeckLinkIterator)
    except Exception as e:
        print(f"[ERROR] Failed to load DeckLink: {e}")
        return False, None, "DeckLink DLL error"

    # Get specific device
    try:
        for i in range(device_index + 1):
            device = iterator.Next()
            if device is None:
                return False, None, f"Device {device_index} not found"
        device_name = device.GetDisplayName()
        print(f"[*] Device: {device_name}\n")
    except Exception as e:
        return False, None, f"Device enumeration failed: {e}"

    # Get input interface
    try:
        decklink_input = device.QueryInterface(decklink_module.IDeckLinkInput)
    except Exception as e:
        return False, None, f"Cannot get IDeckLinkInput: {e}"

    print("[*] Testing signal detection...\n")

    formats_to_test = [
        ("1080i50",    decklink_module.bmdModeHD1080i50),
        ("1080i59.94", decklink_module.bmdModeHD1080i5994),
        ("1080p50",    decklink_module.bmdModeHD1080p50),
        ("1080p59.94", decklink_module.bmdModeHD1080p5994),
        ("720p50",     decklink_module.bmdModeHD720p50),
        ("720p59.94",  decklink_module.bmdModeHD720p5994),
        ("2160p50",    decklink_module.bmdMode4K2160p50),
        ("2160p59.94", decklink_module.bmdMode4K2160p5994),
    ]

    signal_found = False
    format_found = None

    for format_name, format_code in formats_to_test:
        try:
            hr = decklink_input.EnableVideoInput(
                format_code,
                decklink_module.bmdFormat8BitYUV,
                decklink_module.bmdVideoInputEnableFormatDetection,
            )

            if hr == 0:
                print(f"[OK] Format DETECTED: {format_name}")
                signal_found = True
                format_found = format_name

                try:
                    detected_mode = decklink_input.GetDetectedVideoInputMode()
                    if detected_mode != 0:
                        print(f"[OK] Device is actively receiving {format_name} signal")
                except Exception:
                    print(f"    (Enable signal detection for {format_name})")

                decklink_input.DisableVideoInput()
                break
            else:
                print(f"[X] {format_name:20} — not available (hr=0x{hr:08x})")

        except Exception as e:
            error_str = str(e).lower()
            if "already" in error_str or "use" in error_str:
                print(f"[!] {format_name:20} — device locked/in use")
            else:
                print(f"[X] {format_name:20} — error")

    print("\n" + "=" * 70)

    ch_yaml = _FFRECORD_CONFIGS / f"ch{device_index + 1:02d}.yaml"

    if signal_found:
        print(f"\n[OK] SIGNAL DETECTED: {format_found}")
        print("\nNext steps:")
        print(f"  1. If format is NOT what is configured, update {ch_yaml}:")
        print(f"     expected_format: \"{format_found}\"")
        print(f"  2. Restart the recorder")
        return True, format_found, None

    else:
        print("\n[ERROR] NO SIGNAL DETECTED on device\n")
        print("This means:")
        print("  - No video signal reaching the DeckLink device")
        print("  - OR all device inputs are disabled")
        print("  - OR cable/source is not active\n")
        print("TO FIX:")
        print("  1. Verify SDI/HDMI cable is securely connected")
        print("  2. Check that video source is powered on")
        print("  3. Check source is outputting video (check source display/monitor)")
        print("  4. Try a different cable")
        print("  5. Try a different source")
        print("  6. Check Blackmagic Desktop Video Control Panel:")
        print("     - Open Control Panel")
        print("     - Go to Input section")
        print("     - Verify input is enabled (not grayed out)")
        print("     - Check signal status indicator\n")
        return False, None, "No signal on device input"


def main():
    device_index = 0
    if len(sys.argv) > 1:
        try:
            device_index = int(sys.argv[1])
        except ValueError:
            print("Usage: detect_decklink_signal.py [device_index]")
            sys.exit(1)

    signal_found, format_detected, error = detect_signal(device_index)

    if signal_found:
        print(f"\n[OK] Signal detection completed: {format_detected}\n")
        sys.exit(0)
    else:
        print(f"\n[ERROR] {error}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
