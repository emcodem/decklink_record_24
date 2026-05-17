"""OutputThread abstract base — bounded queue, segment policy, encoder auto-restart."""

from __future__ import annotations

import logging
import queue
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from ..sync_log import log_dropped_frame

logger = logging.getLogger(__name__)


@dataclass
class VideoFrame:
    data: np.ndarray           # (H, W, C) uint8 or (H, W*2) UYVY
    fmt: str                   # 'uyvy422' | 'rgb24' | 'yuv420p' etc.
    width: int
    height: int
    framerate: tuple[int, int] # (num, den)
    hw_pts: int = 0
    hw_pts_rate: int = 10_000_000
    hw_pts_valid: bool = False
    timecode: str = ""


@dataclass
class AudioPacket:
    data: np.ndarray           # (samples, channels) int16
    sample_rate: int = 48000
    channels: int = 2
    hw_pts: int = 0
    hw_pts_rate: int = 10_000_000
    hw_pts_valid: bool = False


@dataclass
class OutputStats:
    frames_written: int = 0
    frames_dropped: int = 0
    segments_completed: int = 0
    encoder_restarts: int = 0
    last_error: str = ""
    enabled: bool = True
    paused: bool = False


class OutputThread(ABC):
    """Base class for a recording output running in its own thread.

    The capture thread calls push_video() / push_audio() non-blocking;
    frames that arrive when the queue is full are counted as dropped.
    """

    QUEUE_MAXSIZE = 10
    RESTART_DELAY = 2.0   # seconds between encoder restart attempts
    STATS_INTERVAL = 5.0  # seconds between [stats] heartbeat lines

    def __init__(self, name: str, channel_name: str, segment_seconds: int):
        self.name = name
        self.channel_name = channel_name
        self.segment_seconds = segment_seconds
        self.stats = OutputStats()
        self.video_pkts_muxed = 0   # incremented by subclasses after container.mux()
        self.audio_pkts_muxed = 0
        self.audio_frames_encoded = 0   # number of AudioFrames sent through astream.encode()

        self._video_queue: queue.Queue[Optional[VideoFrame]] = queue.Queue(maxsize=self.QUEUE_MAXSIZE)
        self._audio_queue: queue.Queue[Optional[AudioPacket]] = queue.Queue(maxsize=self.QUEUE_MAXSIZE * 8)
        self._thread: Optional[threading.Thread] = None
        self._stats_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._format_change_pending = threading.Event()
        self._log = logging.getLogger(f"ffrecord.output.{name}")
        self._queue_near_full = False   # True while qsize >= QUEUE_MAXSIZE - 2

    # ── public API (called from capture/service thread) ──────────────────────

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name=f"output-{self.name}", daemon=True)
        self._thread.start()
        self._stats_thread = threading.Thread(target=self._stats_loop, name=f"stats-{self.name}", daemon=True)
        self._stats_thread.start()
        self._log.info("Output thread started")

    def stop(self, timeout: float = 10.0) -> None:
        self._log.info("stop() called")
        self._stop_event.set()
        # Unblock the thread if it's waiting on the queue
        try:
            self._video_queue.put_nowait(None)
        except queue.Full:
            pass
        if self._thread:
            self._log.info("Joining encoder thread with timeout=%.1fs", timeout)
            self._thread.join(timeout=timeout)
            is_alive = self._thread.is_alive()
            self._log.info("Encoder thread join complete, is_alive=%s", is_alive)
        if self._stats_thread:
            self._stats_thread.join(timeout=2.0)
        self._log.info("Output thread stopped (frames=%d dropped=%d segments=%d)",
                       self.stats.frames_written, self.stats.frames_dropped,
                       self.stats.segments_completed)

    # ── stats heartbeat ─────────────────────────────────────────────────────

    def _stats_loop(self) -> None:
        last_v_frames = 0
        last_a_frames = 0
        last_dropped = 0
        last_v_mux = 0
        last_a_mux = 0
        last_time = time.monotonic()
        while not self._stop_event.wait(timeout=self.STATS_INTERVAL):
            now = time.monotonic()
            dt = now - last_time
            vframes = self.stats.frames_written
            aframes = self.audio_frames_encoded
            dropped = self.stats.frames_dropped
            v_mux = self.video_pkts_muxed
            a_mux = self.audio_pkts_muxed
            dvf = vframes - last_v_frames
            daf = aframes - last_a_frames
            dd = dropped - last_dropped
            dv_mux = v_mux - last_v_mux
            da_mux = a_mux - last_a_mux
            fps = dvf / dt if dt > 0 else 0.0
            self._log.info(
                "[stats] video: encoded=%d (+%d, %.1f fps) muxed=%d (+%d) dropped=%d (+%d) | "
                "audio: encoded=%d (+%d) muxed=%d (+%d) | "
                "segments=%d restarts=%d vq=%d/%d aq=%d/%d enabled=%s paused=%s",
                vframes, dvf, fps, v_mux, dv_mux, dropped, dd,
                aframes, daf, a_mux, da_mux,
                self.stats.segments_completed,
                self.stats.encoder_restarts,
                self._video_queue.qsize(), self.QUEUE_MAXSIZE,
                self._audio_queue.qsize(), self.QUEUE_MAXSIZE * 8,
                self.stats.enabled, self.stats.paused,
            )
            last_v_frames = vframes
            last_a_frames = aframes
            last_dropped = dropped
            last_v_mux = v_mux
            last_a_mux = a_mux
            last_time = now

    def push_video(self, frame: VideoFrame) -> None:
        if not self.stats.enabled or self.stats.paused:
            return
        qsize = self._video_queue.qsize()
        near_full = qsize >= self.QUEUE_MAXSIZE - 2
        if near_full and not self._queue_near_full:
            self._log.warning(
                "[sync] QUEUE_NEAR_FULL output=%s qsize=%d/%d — encoder falling behind",
                self.name, qsize, self.QUEUE_MAXSIZE,
            )
            self._queue_near_full = True
        elif not near_full and self._queue_near_full:
            self._log.info(
                "[sync] QUEUE_RECOVERED output=%s qsize=%d/%d",
                self.name, qsize, self.QUEUE_MAXSIZE,
            )
            self._queue_near_full = False
        try:
            self._video_queue.put_nowait(frame)
        except queue.Full:
            self.stats.frames_dropped += 1
            count = self.stats.frames_dropped
            if count in (1, 10, 100, 1000) or (count > 1000 and count % 1000 == 0):
                log_dropped_frame(self.name, count)

    def push_audio(self, pkt: AudioPacket) -> None:
        if not self.stats.enabled or self.stats.paused:
            return
        try:
            self._audio_queue.put_nowait(pkt)
        except queue.Full:
            pass   # audio drops are not counted; video frame count is the primary metric

    def set_enabled(self, enabled: bool) -> None:
        self.stats.enabled = enabled
        self._log.info("Output %s: enabled=%s", self.name, enabled)

    def set_paused(self, paused: bool) -> None:
        self.stats.paused = paused
        self._log.info("Output %s: paused=%s", self.name, paused)

    def notify_format_change(self) -> None:
        """Signal that a DeckLink format change occurred; triggers A/V realignment on first frame."""
        self._format_change_pending.set()

    # ── internal run loop ───────────────────────────────────────────────────

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._encoder_loop()
            except Exception as e:
                self.stats.encoder_restarts += 1
                self.stats.last_error = str(e)
                self._log.error("Encoder crashed (restart #%d): %s", self.stats.encoder_restarts, e, exc_info=True)
                if not self._stop_event.is_set():
                    time.sleep(self.RESTART_DELAY)
                    self._log.info("Restarting encoder...")

    def _get_video(self, timeout: float = 1.0) -> Optional[VideoFrame]:
        try:
            frame = self._video_queue.get(timeout=timeout)
            return frame   # None is the stop sentinel
        except queue.Empty:
            return None

    def _drain_audio(self) -> list[AudioPacket]:
        pkts = []
        while True:
            try:
                pkt = self._audio_queue.get_nowait()
                pkts.append(pkt)
            except queue.Empty:
                break
        return pkts

    @abstractmethod
    def _encoder_loop(self) -> None:
        """Open container, encode frames, manage segments, flush on exit."""
        ...
