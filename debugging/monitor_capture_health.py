#!/usr/bin/env python3
"""
monitor_capture_health.py — Real-time ffrecord capture health monitor

Tails ffrecord log files and alerts when a channel reports 0 fps for longer
than a configurable threshold.  This catches "silent failures" where the
DeckLink device appears initialized but is not delivering frames (e.g. no
signal, format mismatch, or a driver stall).

Log structure expected (written by ffrecord's sync_log):
    [stats] video: N frames (+N, N fps)

One subdirectory per channel is expected inside the logs directory, e.g.:
    logs/ch01/ffrecord_YYYYMMDD.log
    logs/ch02/ffrecord_YYYYMMDD.log

Usage:
    python monitor_capture_health.py [logs_dir] [zero_fps_alert_seconds]

    logs_dir               path to the logs directory
                           (default: C:\\dev\\cmd_recorder\\logs)
    zero_fps_alert_seconds seconds of 0 fps before an alert is printed
                           (default: 30)
"""

import os
import sys
import time
from pathlib import Path
from collections import defaultdict
from datetime import datetime

_DEFAULT_LOGS_DIR = r"C:\dev\cmd_recorder\logs"
_DEBUGGING_DIR = Path(__file__).resolve().parent


def monitor_logs(logs_dir, check_interval=5, zero_fps_threshold=30):
    print("=" * 80)
    print("FFRecord Capture Health Monitor")
    print("=" * 80)
    print(f"\nWatching: {logs_dir}")
    print(f"Check interval: {check_interval}s")
    print(f"Alert threshold: {zero_fps_threshold}s of 0 fps\n")
    print("Press Ctrl+C to exit\n")

    logs_path = Path(logs_dir)
    if not logs_path.exists():
        print(f"[ERROR] Logs directory not found: {logs_dir}")
        print(f"  Pass the correct path as the first argument, e.g.:")
        print(f"  python {Path(__file__).name} C:\\dev\\cmd_recorder\\logs")
        return

    channel_state = defaultdict(lambda: {
        "last_check": None,
        "zero_fps_start": None,
        "zero_fps_duration": 0,
        "alerted": False,
        "last_file": None,
        "last_position": 0,
    })

    try:
        while True:
            now = datetime.now()

            for channel_dir in logs_path.iterdir():
                if not channel_dir.is_dir():
                    continue

                channel_name = channel_dir.name
                log_files = list(channel_dir.glob("*.log"))
                if not log_files:
                    continue

                log_file = sorted(log_files, key=lambda f: f.stat().st_mtime)[-1]
                state = channel_state[channel_name]

                if state["last_file"] == str(log_file) and state["last_position"] > 0:
                    try:
                        file_size = log_file.stat().st_size
                        if file_size <= state["last_position"]:
                            continue
                    except Exception:
                        continue

                try:
                    with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(state["last_position"])
                        new_lines = f.readlines()
                        state["last_position"] = f.tell()
                        state["last_file"] = str(log_file)
                except Exception:
                    continue

                for line in new_lines:
                    if "[stats]" in line and "video:" in line:
                        if "fps)" in line:
                            try:
                                fps_start = line.index("(") + 1
                                fps_end = line.index(" fps)")
                                fps_str = line[fps_start:fps_end].strip()
                                fps = float(fps_str.split()[-1])

                                if fps == 0.0:
                                    if state["zero_fps_start"] is None:
                                        state["zero_fps_start"] = now
                                    state["zero_fps_duration"] = (now - state["zero_fps_start"]).total_seconds()

                                    if state["zero_fps_duration"] >= zero_fps_threshold and not state["alerted"]:
                                        _print_alert(channel_name, state["zero_fps_duration"], line)
                                        state["alerted"] = True
                                else:
                                    if state["zero_fps_start"] is not None:
                                        print(f"[{now.strftime('%H:%M:%S')}] {channel_name} RECOVERED — {fps:.1f} fps")
                                    state["zero_fps_start"] = None
                                    state["zero_fps_duration"] = 0
                                    state["alerted"] = False
                            except (ValueError, IndexError):
                                pass

                    elif "DeckLink capture started" in line:
                        print(f"[{now.strftime('%H:%M:%S')}] {channel_name} capture initialized")
                        state["zero_fps_start"] = now
                        state["alerted"] = False

            time.sleep(check_interval)

    except KeyboardInterrupt:
        print("\n\n[STOPPED] Monitor exited")


def _print_alert(channel, duration, last_line):
    now = datetime.now()
    print("\n" + "=" * 80)
    print(f"[ALERT] {now.strftime('%H:%M:%S')} — {channel} has 0 fps for {duration:.0f}+ seconds")
    print("=" * 80)
    print("\n[!!] SILENT FAILURE: Device initialized but not capturing frames\n")

    print("LIKELY CAUSES (check in this order):\n")

    print("1. NO VIDEO SIGNAL on input")
    print("   - Check SDI/HDMI cable connected to device input")
    print("   - Verify video source is powered on and active")
    print("   - Try disconnecting and reconnecting the cable\n")

    print("2. FORMAT MISMATCH — device receiving a different format than configured")
    print("   - Open Blackmagic Desktop Video Control Panel")
    print("   - Check what format the device is detecting")
    print("   - If different, update the relevant ffrecord YAML config\n")

    print("3. DEVICE IN USE by another application")
    print(f"   - Run: python {_DEBUGGING_DIR}\\check_decklink_availability.py")
    print("   - Close any competing apps (OBS, Resolve, Media Express, etc.)\n")

    print("IMMEDIATE DIAGNOSTICS:")
    print(f"  python {_DEBUGGING_DIR}\\check_decklink_in_use.py")
    print(f"  python {_DEBUGGING_DIR}\\diagnose_decklink.py")
    print(f"  Check logs: {Path(last_line).parent if False else 'see logs dir above'}\n")

    print("Last log line:")
    print(f"  {last_line.strip()}\n")
    print("=" * 80 + "\n")


def main():
    logs_dir = sys.argv[1] if len(sys.argv) > 1 else _DEFAULT_LOGS_DIR
    threshold = int(sys.argv[2]) if len(sys.argv) > 2 else 30

    try:
        monitor_logs(logs_dir, zero_fps_threshold=threshold)
    except Exception as e:
        print(f"[ERROR] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
