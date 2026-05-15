"""ffrecord entry point.

Usage:
    ffrecord --config config\\example.yaml [--list-devices]
    python -m ffrecord.main --config config\\example.yaml
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
import logging

from .config import load_config
from .http_server import HttpServer
from .logging_setup import setup_logging
from .service import Service


def _list_devices() -> None:
    """Print available DeckLink devices and exit."""
    try:
        from comtypes.client import GetModule, CreateObject
        dll = r"C:\Program Files\Blackmagic Design\Blackmagic Desktop Video\DeckLinkAPI64.dll"
        m = GetModule(dll)
        it = CreateObject(m.CDeckLinkIterator)
        idx = 0
        while True:
            try:
                dev = it.Next()
                if dev is None:
                    break
                name = dev.GetDisplayName()
                print(f"  [{idx}] {name}")
                idx += 1
            except Exception:
                break
        if idx == 0:
            print("  No DeckLink devices found.")
    except Exception as e:
        print(f"  Error enumerating devices: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description="ffrecord — 24/7 SDI recording service")
    parser.add_argument("--config", required=False, default=None,
                        help="Path to YAML config file (required unless --list-devices)")
    parser.add_argument("--list-devices", action="store_true",
                        help="List available DeckLink devices and exit")
    args = parser.parse_args()

    if args.list_devices:
        print("Available DeckLink devices:")
        _list_devices()
        sys.exit(0)

    if not args.config:
        parser.error("--config is required")

    cfg = load_config(args.config)

    setup_logging(
        channel_name=cfg.channel.name,
        log_dir=cfg.logging.dir,
        rotation_days=cfg.logging.file_rotation_days,
        level=cfg.logging.level,
    )

    log = logging.getLogger("ffrecord.main")
    log.info("ffrecord starting — channel=%s device=%d", cfg.channel.name, cfg.channel.decklink_device_index)
    log.info("Config: %d output(s) configured", len(cfg.outputs))
    for out in cfg.outputs:
        log.info("  Output %-20s  type=%-4s  enabled=%s  segment=%ds",
                 out.name, out.type, out.enabled, out.segment_seconds)

    service = Service(cfg)
    http = HttpServer(cfg.http.bind, cfg.http.port, service)

    # ── signal handlers ──────────────────────────────────────────────────────

    def _shutdown(signum, frame):
        log.info("Received signal %d — shutting down...", signum)
        service.stop()
        http.stop()
        sys.exit(0)

    def _reload(signum, frame):
        log.info("Received SIGHUP — reloading config from %s", args.config)
        try:
            new_cfg = load_config(args.config)
            service.reload_config(new_cfg)
        except Exception as e:
            log.error("Config reload failed: %s", e)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    # SIGHUP is Unix-only; skip on Windows (Windows uses Ctrl+C → SIGINT)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _reload)

    # ── start ────────────────────────────────────────────────────────────────

    try:
        service.start()
        http.start()
        log.info("ffrecord running. HTTP status at http://%s:%d/status", cfg.http.bind, cfg.http.port)

        while True:
            time.sleep(60)

    except KeyboardInterrupt:
        log.info("Keyboard interrupt — shutting down...")
    except Exception as e:
        log.critical("Fatal error during startup or run: %s", e, exc_info=True)
        sys.exit(1)
    finally:
        service.stop()
        http.stop()


if __name__ == "__main__":
    main()
