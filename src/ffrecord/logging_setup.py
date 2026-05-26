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

    _setup_libav_logging()


def _setup_libav_logging() -> None:
    """Route libav's internal av_log() output into Python logging.

    Without this, a libav failure surfaces only as a bare errno on the raised
    exception (e.g. "Invalid argument ... returned 22" = EINVAL), and the
    descriptive text the muxer/encoder emits via av_log — e.g. "Application
    provided invalid, non monotonically increasing dts to muxer in stream 0" —
    is silently dropped. PyAV forwards av_log to Python loggers named
    'libav.<component>' (libav.mov, libav.libx264, ...) once a capture level is
    set; those propagate to the root handler configured above.

    Level WARNING keeps errors + warnings but suppresses the chatty per-frame
    INFO/VERBOSE output libx264 emits.
    """
    try:
        import av.logging as avlog
        avlog.set_level(avlog.WARNING)
    except Exception as e:  # pragma: no cover - libav binding optional at import
        logging.getLogger("ffrecord").warning(
            "Could not configure libav logging bridge: %s", e,
        )
