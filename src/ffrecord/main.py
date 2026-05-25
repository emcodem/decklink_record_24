"""ffrecord entry point.

Usage:
    ffrecord --config config\\example.yaml [--list-devices]
    python -m ffrecord.main --config config\\example.yaml
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
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
        splitter = (
            f"app/{out.internal_splitter.seconds}s" if out.internal_splitter.enabled
            else "libav"
        )
        log.info(
            "  Output %-20s  container=%-8s  enabled=%s  splitter=%s",
            out.name, out.container_format, out.enabled, splitter,
        )

    service = Service(cfg)
    _shutdown_event = threading.Event()

    http = HttpServer(cfg.http.bind, cfg.http.port, service, on_shutdown=lambda: _shutdown(None, None))

    # ── signal handlers ──────────────────────────────────────────────────────

    def _shutdown(signum, frame):
        sig_str = f"signal {signum}" if signum is not None else "HTTP /stop"
        log.info("Shutting down (%s)...", sig_str)
        _shutdown_event.set()

    # Config changes require a process restart — no runtime reload.
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    # Windows console control events — only register if supported
    if hasattr(signal, "CTRL_C_EVENT"):
        try:
            signal.signal(signal.CTRL_C_EVENT, _shutdown)
        except (ValueError, RuntimeError):
            pass
    if hasattr(signal, "CTRL_BREAK_EVENT"):
        try:
            signal.signal(signal.CTRL_BREAK_EVENT, _shutdown)
        except (ValueError, RuntimeError):
            pass

    # ── start ────────────────────────────────────────────────────────────────

    try:
        service.start()
        http.start()
        log.info("ffrecord running. HTTP status at http://%s:%d/status", cfg.http.bind, cfg.http.port)

        _shutdown_event.wait()
        log.info("Shutdown initiated — stopping service...")

    except KeyboardInterrupt:
        log.info("Keyboard interrupt — shutting down...")
    except Exception as e:
        log.critical("Fatal error during startup or run: %s", e, exc_info=True)
        sys.exit(1)
    finally:
        service.stop()
        http.stop()
        for h in log.handlers:
            h.flush()


if __name__ == "__main__":
    main()
