"""Service — lifecycle orchestration for one ffrecord channel instance.

Responsibilities:
  - Open the DeckLink device (COM-only).
  - Build the capture filter graph (if configured).
  - Start N output threads from config.
  - Fan-out incoming frames/audio to per-output bounded queues.
  - Monitor disk free space and pause/resume outputs accordingly.
  - Handle format-change callbacks from DeckLink (treat as signal-loss: close
    all segments, reinit filter graph, start new segments).
  - Expose status, pause/resume, and per-output enable/disable for the HTTP server.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np

from .capture.input_filter import InputVideoFilter
from .capture.decklink_com import DeckLinkCapture
from .config import ServiceConfig, OutputConfig
from .output.base import AudioPacket, OutputThread, VideoFrame
from .output.file_output import FileOutput
from .output.hls_output import HlsOutput
from .sync_log import log_signal_loss

logger = logging.getLogger("ffrecord.service")

DISK_CHECK_INTERVAL = 30.0     # seconds between free-space checks
DISK_PAUSE_THRESHOLD_GB = 5.0  # pause writes below this free space
DISK_RESUME_THRESHOLD_GB = 10.0


def _build_output(cfg: OutputConfig, channel_name: str) -> OutputThread:
    if cfg.type == "hls":
        return HlsOutput(cfg, channel_name)
    return FileOutput(cfg, channel_name)


class Service:
    def __init__(self, config: ServiceConfig):
        self.config = config
        self.channel_name = config.channel.name
        self._outputs: list[OutputThread] = []
        self._capture: Optional[DeckLinkCapture] = None
        self._video_filter: Optional[InputVideoFilter] = None
        self._stop_event = threading.Event()
        self._disk_paused = False
        self._global_paused = False
        self._lock = threading.Lock()
        self._frame_count = 0
        self._last_format: Optional[str] = None

    def start(self) -> None:
        logger.info("Service starting for channel %s", self.channel_name)

        self._outputs = [
            _build_output(cfg, self.channel_name)
            for cfg in self.config.outputs
            if cfg.enabled
        ]
        for out in self._outputs:
            out.start()

        self._capture = DeckLinkCapture(
            device_index=self.config.channel.decklink_device_index,
            audio_channels=self.config.capture.audio_channels,
            format_change_callback=self._on_format_change,
        )
        self._capture.start(
            frame_callback=self._on_video_frame,
            audio_callback=self._on_audio_packet,
        )

        disk_thread = threading.Thread(target=self._disk_monitor_loop, name="disk-monitor", daemon=True)
        disk_thread.start()

        logger.info("Service started (%d outputs)", len(self._outputs))

    def stop(self) -> None:
        logger.info("Service stopping...")
        self._stop_event.set()

        if self._capture:
            self._capture.stop()

        for out in self._outputs:
            out.stop()

        if self._video_filter:
            self._video_filter.close()

        logger.info("Service stopped")

    # ── capture callbacks ────────────────────────────────────────────────────

    def _on_video_frame(self, frame_bytes: bytes, width: int, height: int,
                        pixel_format: int, framerate: tuple, flags: int, row_bytes: int,
                        stream_time: int, timescale: int, stream_time_valid: bool,
                        hw_ref_time: int, hw_ref_valid: bool, tc_str: str) -> None:
        if self._global_paused or self._disk_paused:
            return

        cap = self.config.capture

        # Lazy-init the filter on first frame — only build a graph when video_filter
        # is set. Empty video_filter ⇒ passthrough (raw uyvy422 to outputs).
        if self._video_filter is None:
            self._video_filter = InputVideoFilter(
                width, height, framerate,
                spec=cap.video_filter,
                pix_fmt=cap.pix_fmt,
            )

        hw_pts = stream_time
        hw_pts_valid = stream_time_valid

        # The filter (or passthrough) yields its output frames; its `output_*` fields
        # report what the encoder downstream should expect (size, framerate, pix_fmt).
        vf_ = self._video_filter
        frames_out = list(vf_.process(frame_bytes, width, height, row_bytes))
        out_w = vf_.output_width
        out_h = vf_.output_height
        out_fr = vf_.output_framerate
        out_fmt = vf_.output_pix_fmt

        for arr in frames_out:
            video_frame = VideoFrame(
                data=arr,
                fmt=out_fmt,
                width=out_w,
                height=out_h,
                framerate=out_fr,
                hw_pts=hw_pts,
                hw_pts_rate=timescale,
                hw_pts_valid=hw_pts_valid,
                timecode=tc_str,
            )
            for out in self._outputs:
                out.push_video(video_frame)

        self._frame_count += 1

    def _on_audio_packet(self, audio_arr: np.ndarray, sample_rate: int, channels: int,
                         hw_pts: int, timescale: int, hw_pts_valid: bool) -> None:
        if self._global_paused or self._disk_paused:
            return
        pkt = AudioPacket(
            data=audio_arr,
            sample_rate=sample_rate,
            channels=channels,
            hw_pts=hw_pts,
            hw_pts_rate=timescale,
            hw_pts_valid=hw_pts_valid,
        )
        for out in self._outputs:
            out.push_audio(pkt)

    def _on_format_change(self, new_format: str) -> None:
        logger.warning("Format change detected: %s → %s. Treating as signal-loss: resetting filter graph.", self._last_format, new_format)
        log_signal_loss(f"format_change old={self._last_format} new={new_format}")
        self._last_format = new_format
        # Reset filter — it will be recreated on the next frame with new geometry/rate
        if self._video_filter:
            self._video_filter.close()
            self._video_filter = None
        # Notify outputs to realign audio/video after the format change
        for out in self._outputs:
            out.notify_format_change()

    # ── disk monitoring ──────────────────────────────────────────────────────

    def _disk_monitor_loop(self) -> None:
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=DISK_CHECK_INTERVAL)
            if self._stop_event.is_set():
                break
            self._check_disk()
            self._check_signal_lock()

    def _check_signal_lock(self) -> None:
        if self._capture is None:
            return
        try:
            locked = self._capture.get_signal_locked()
            if locked is False:
                log_signal_loss("signal_not_locked (periodic health check — no SDI signal)")
        except Exception as e:
            logger.debug("Signal lock check failed: %s", e)

    def _check_disk(self) -> None:
        try:
            usage = shutil.disk_usage(self.config.logging.dir)
            free_gb = usage.free / (1024 ** 3)
            if not self._disk_paused and free_gb < DISK_PAUSE_THRESHOLD_GB:
                self._disk_paused = True
                logger.error(
                    "[disk] Free space %.1f GB below pause threshold %.1f GB — pausing all writes",
                    free_gb, DISK_PAUSE_THRESHOLD_GB,
                )
            elif self._disk_paused and free_gb >= DISK_RESUME_THRESHOLD_GB:
                self._disk_paused = False
                logger.info(
                    "[disk] Free space %.1f GB above resume threshold %.1f GB — resuming writes",
                    free_gb, DISK_RESUME_THRESHOLD_GB,
                )
        except Exception as e:
            logger.error("Disk check failed: %s", e)

    # ── HTTP control API ─────────────────────────────────────────────────────

    def set_global_pause(self, paused: bool) -> None:
        with self._lock:
            self._global_paused = paused
            for out in self._outputs:
                out.set_paused(paused)
        logger.info("Global pause: %s", paused)

    def set_output_enabled(self, name: str, enabled: bool) -> bool:
        for out in self._outputs:
            if out.name == name:
                out.set_enabled(enabled)
                return True
        return False

    def get_status(self) -> dict:
        usage = None
        try:
            u = shutil.disk_usage(self.config.logging.dir)
            usage = {"free_gb": round(u.free / 1024**3, 2), "total_gb": round(u.total / 1024**3, 2)}
        except Exception:
            pass

        return {
            "channel": self.channel_name,
            "capture": self._capture.get_status() if self._capture else {},
            "global_paused": self._global_paused,
            "disk_paused": self._disk_paused,
            "frames_captured": self._frame_count,
            "disk": usage,
            "outputs": [
                {
                    "name": out.name,
                    "enabled": out.stats.enabled,
                    "paused": out.stats.paused,
                    "frames_written": out.stats.frames_written,
                    "frames_dropped": out.stats.frames_dropped,
                    "segments_completed": out.stats.segments_completed,
                    "encoder_restarts": out.stats.encoder_restarts,
                    "last_error": out.stats.last_error,
                }
                for out in self._outputs
            ],
        }

    def reload_config(self, new_config: ServiceConfig) -> None:
        """Apply safe runtime config changes (enable/disable outputs, segment duration).
        Codec/container changes require a full restart.
        """
        logger.info("Reloading config (safe changes only)")
        existing = {out.name: out for out in self._outputs}
        for cfg in new_config.outputs:
            if cfg.name in existing:
                existing[cfg.name].set_enabled(cfg.enabled)
                existing[cfg.name].segment_seconds = cfg.segment_seconds
        self.config = new_config
