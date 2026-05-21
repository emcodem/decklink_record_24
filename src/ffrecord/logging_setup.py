"""Logging setup: rotating file per channel + stderr for supervisor."""

import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path


def setup_logging(channel_name: str, log_dir: str, rotation_days: int, level: str = "INFO") -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    log_file = log_path / f"ffrecord_{channel_name}.log"

    root = logging.getLogger()
    root.setLevel(log_level)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_file,
        when="midnight",
        backupCount=rotation_days,
        encoding="utf-8",
        utc=True,
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(fmt)
    root.addHandler(stderr_handler)

    # Route unhandled worker-thread exceptions into the log file.
    # Without this, Python prints them only to stderr (which the supervisor
    # discards), so crashes in capture/output threads vanish silently.
    _thread_log = logging.getLogger("ffrecord.thread")

    def _thread_excepthook(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit:
            return
        _thread_log.critical(
            "Unhandled exception in thread '%s'",
            args.thread.name if args.thread else "<unknown>",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = _thread_excepthook
