"""OutputThread abstract base — single AVPair queue + encoder auto-restart.

Each output (file, HLS) consumes pre-paired AVPair objects produced by
CaptureBuffer. The previous design used separate video/audio queues and an
"audio drain" model that produced a variable A/V offset per segment; that
is gone now. Each pair carries video + exactly the audio that temporally
matches its frame duration, so the encoder's PTS counters (still zero-based
per segment) always describe a tight A/V alignment.

Drop policy: when the per-output EncodingBuffer is full, the incoming pair
is dropped and counted. The encoder thread is the sole consumer.
"""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from ..encoding_buffer import EncodingBuffer
from ..sync_log import log_dropped_frame, log_encoder_persistent_crash, log_join_timeout
from .util import MuxCounters

logger = logging.getLogger(__name__)


# ── A/V data types ─────────────────────────────────────────────────────────


@dataclass
class VideoFrame:
    data: np.ndarray           # (H, W, C) uint8 or (H, W*2) UYVY — for code paths that need numpy
    fmt: str                   # 'uyvy422' | 'rgb24' | 'yuv420p' etc.
    width: int
    height: int
    framerate: tuple[int, int] # (num, den)
    hw_pts: int = 0
    hw_pts_rate: int = 10_000_000
    hw_pts_valid: bool = False
    timecode: str = ""
    interlaced_frame: bool = False
    top_field_first: bool = False
    # The original AVFrame from the InputVideoFilter (with correct interlaced
    # metadata set by setfield/yadif/etc). Encoder paths that want to preserve
    # field metadata should use this directly via av_frame.encode() instead of
    # av.VideoFrame.from_ndarray(data) — which loses interlace flags AND requires
    # a downstream setfield filter that crashes at full-HD in PyAV 17 under
    # multi-thread load. May be None for paths that strip metadata intentionally.
    av_frame: object = None  # av.VideoFrame, untyped to avoid import here


@dataclass
class AudioPacket:
    data: np.ndarray           # (samples, channels) int16
    sample_rate: int = 48000
    channels: int = 2
    hw_pts: int = 0
    hw_pts_rate: int = 10_000_000
    hw_pts_valid: bool = False


@dataclass
class AVPair:
    """One video frame paired with exactly the audio for its duration.

    audio.data is always sized to match video frame_duration_samples
    (CaptureBuffer pads with silence on partial extraction). When
    audio_is_synthesized is True, audio.data is all zeros — emitted because
    audio was missing, late, or skipped over.
    """
    video: VideoFrame
    audio: AudioPacket
    audio_is_synthesized: bool = False


# ── stats ──────────────────────────────────────────────────────────────────


@dataclass
class OutputStats:
    frames_written: int = 0
    frames_dropped: int = 0
    segments_completed: int = 0
    encoder_restarts: int = 0
    last_error: str = ""
    enabled: bool = True
    paused: bool = False
    synthesized_audio_frames: int = 0   # AVPair.audio_is_synthesized=True count
    mux_failures: int = 0               # total container.mux() failures (B2)


# ── base class ─────────────────────────────────────────────────────────────


# Per-output pair queue capacity. 500 slots covers 10s at 50fps, 20s at 25fps,
# 16.7s at 29.97fps, and 8.3s at 60fps — comfortable headroom for any encoder
# stall short of a multi-minute disk hang. At ~3 MB/frame this is ~1.5 GB worst
# case per queue (refs are shared with other outputs of the same channel, so
# total channel memory ≈ frames_in_flight × frame_size, not multiplied per output).
DEFAULT_QUEUE_CAPACITY = 500


class OutputThread(ABC):
    """Base class for a recording output running in its own thread.

    The capture/pairing layer calls push_pair() non-blocking; pairs that
    arrive when the queue is full are counted as drops.
    """

    # Exponential backoff for encoder restarts. The encoder always keeps trying
    # (broadcast = best-effort, never give up), but the gap between attempts
    # widens so a persistently-broken codec doesn't burn 100% CPU spinning.
    RESTART_DELAY_INITIAL = 2.0
    RESTART_DELAY_MAX = 60.0
    # Window in which N+ restarts trigger a single PERSISTENT_CRASH ERROR line
    # so the operator sees the recurring failure clearly above WARNING noise.
    PERSISTENT_CRASH_WINDOW_S = 300.0
    PERSISTENT_CRASH_THRESHOLD = 5
    STATS_INTERVAL = 5.0  # seconds between [stats] heartbeat lines

    def __init__(self, name: str, channel_name: str, segment_seconds: int):
        self.name = name
        self.channel_name = channel_name
        self.segment_seconds = segment_seconds
        self.stats = OutputStats()
        self.video_pkts_muxed = 0    # incremented by subclasses after container.mux()
        self.audio_pkts_muxed = 0
        self.audio_frames_encoded = 0   # number of AudioFrames sent through astream.encode()
        # B2: mux failure counters. EncoderOutput passes this to util.mux_with_logging().
        self.mux_counters = MuxCounters()

        self._pair_buffer: EncodingBuffer[AVPair] = EncodingBuffer(
            name=f"{channel_name}/{name}", capacity=DEFAULT_QUEUE_CAPACITY,
        )

        self._thread: Optional[threading.Thread] = None
        self._stats_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._format_change_pending = threading.Event()
        self._log = logging.getLogger(f"ffrecord.output.{name}")

    def prewarm_codec(
        self,
        enc_w: int,
        enc_h: int,
        enc_fr: tuple,
        enc_pix_fmt: str,
    ) -> None:
        """Optional hook: open the container + codec early so DeckLink isn't
        blocked by avcodec_open2() on the first real frame. Default is a no-op
        for output classes that don't need it.
        """
        return None

    # ── public API (called from capture/pairing thread) ──────────────────────

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name=f"output-{self.name}", daemon=True)
        self._thread.start()
        self._stats_thread = threading.Thread(target=self._stats_loop, name=f"stats-{self.name}", daemon=True)
        self._stats_thread.start()
        self._log.info(
            "Output thread started (pair_queue_capacity=%d)",
            self._pair_buffer.capacity,
        )

    def stop(self, timeout: float = 10.0) -> None:
        self._log.info("stop() called")
        self._stop_event.set()
        # Unblock the thread if it's waiting on the queue
        self._pair_buffer.push_sentinel()
        if self._thread:
            self._log.info("Joining encoder thread with timeout=%.1fs", timeout)
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                log_join_timeout(f"output-{self.name}", timeout)
                self._log.warning("Encoder thread did not exit within %.1fs", timeout)
            else:
                self._log.info("Encoder thread joined cleanly")
        if self._stats_thread:
            self._stats_thread.join(timeout=2.0)
            if self._stats_thread.is_alive():
                log_join_timeout(f"stats-{self.name}", 2.0)
        self._log.info(
            "Output thread stopped (frames=%d dropped=%d segments=%d "
            "synthesized_audio=%d mux_failures=%d)",
            self.stats.frames_written, self.stats.frames_dropped,
            self.stats.segments_completed, self.stats.synthesized_audio_frames,
            self.stats.mux_failures,
        )

    def push_pair(self, pair: AVPair) -> None:
        """Enqueue a paired (video, audio) AVPair for encoding."""
        if not self.stats.enabled or self.stats.paused:
            return
        ok = self._pair_buffer.push(pair)
        if not ok:
            self.stats.frames_dropped += 1
            log_dropped_frame(self.name, self.stats.frames_dropped)

    def set_enabled(self, enabled: bool) -> None:
        self.stats.enabled = enabled
        self._log.info("Output %s: enabled=%s", self.name, enabled)

    def set_paused(self, paused: bool) -> None:
        self.stats.paused = paused
        self._log.info("Output %s: paused=%s", self.name, paused)

    def notify_format_change(self) -> None:
        """Signal that a DeckLink format change occurred; encoder may want to flush."""
        self._format_change_pending.set()

    # ── stats heartbeat ──────────────────────────────────────────────────────

    def _stats_loop(self) -> None:
        last_v_frames = 0
        last_a_frames = 0
        last_dropped = 0
        last_v_mux = 0
        last_a_mux = 0
        last_synthesized = 0
        last_time = time.monotonic()
        while not self._stop_event.wait(timeout=self.STATS_INTERVAL):
            now = time.monotonic()
            dt = now - last_time
            vframes = self.stats.frames_written
            aframes = self.audio_frames_encoded
            dropped = self.stats.frames_dropped
            v_mux = self.video_pkts_muxed
            a_mux = self.audio_pkts_muxed
            synthesized = self.stats.synthesized_audio_frames
            dvf = vframes - last_v_frames
            daf = aframes - last_a_frames
            dd = dropped - last_dropped
            dv_mux = v_mux - last_v_mux
            da_mux = a_mux - last_a_mux
            d_syn = synthesized - last_synthesized
            fps = dvf / dt if dt > 0 else 0.0
            buf_stats = self._pair_buffer.stats()
            self._log.info(
                "[stats] video: encoded=%d (+%d, %.1f fps) muxed=%d (+%d) dropped=%d (+%d) | "
                "audio: encoded=%d (+%d) muxed=%d (+%d) synthesized=%d (+%d) | "
                "segments=%d restarts=%d pair_q=%d/%d peak=%d enabled=%s paused=%s",
                vframes, dvf, fps, v_mux, dv_mux, dropped, dd,
                aframes, daf, a_mux, da_mux, synthesized, d_syn,
                self.stats.segments_completed,
                self.stats.encoder_restarts,
                buf_stats["qsize"], buf_stats["capacity"], buf_stats["qsize_peak"],
                self.stats.enabled, self.stats.paused,
            )
            last_v_frames = vframes
            last_a_frames = aframes
            last_dropped = dropped
            last_v_mux = v_mux
            last_a_mux = a_mux
            last_synthesized = synthesized
            last_time = now

    # ── internal run loop ────────────────────────────────────────────────────

    def _run(self) -> None:
        # B3: exponential backoff between restart attempts and a single ERROR
        # line per persistent-crash episode (>= PERSISTENT_CRASH_THRESHOLD
        # restarts within PERSISTENT_CRASH_WINDOW_S).
        next_delay = self.RESTART_DELAY_INITIAL
        recent_restart_times: list[float] = []
        persistent_crash_logged = False

        while not self._stop_event.is_set():
            try:
                self._encoder_loop()
            except Exception as e:
                now = time.monotonic()
                self.stats.encoder_restarts += 1
                self.stats.last_error = str(e)
                self._log.error(
                    "Encoder crashed (restart #%d, next retry in %.1fs): %s",
                    self.stats.encoder_restarts, next_delay, e, exc_info=True,
                )

                # Slide the restart-time window. If the count inside the window
                # crosses the threshold, log ONE PERSISTENT_CRASH line; reset
                # the flag once the rate calms down so a later relapse logs
                # again.
                cutoff = now - self.PERSISTENT_CRASH_WINDOW_S
                recent_restart_times = [t for t in recent_restart_times if t >= cutoff]
                recent_restart_times.append(now)
                if len(recent_restart_times) >= self.PERSISTENT_CRASH_THRESHOLD:
                    if not persistent_crash_logged:
                        log_encoder_persistent_crash(
                            self.name, self.stats.encoder_restarts,
                            self.PERSISTENT_CRASH_WINDOW_S, str(e),
                        )
                        persistent_crash_logged = True
                else:
                    persistent_crash_logged = False

                if not self._stop_event.is_set():
                    # Wait via the stop event so a shutdown during backoff
                    # exits promptly instead of dragging the full delay.
                    if self._stop_event.wait(timeout=next_delay):
                        break
                    self._log.info("Restarting encoder...")
                    next_delay = min(next_delay * 2.0, self.RESTART_DELAY_MAX)
            else:
                # Encoder loop returned cleanly (no exception). Reset the
                # backoff so an isolated future crash doesn't inherit a long
                # delay from a previous storm.
                next_delay = self.RESTART_DELAY_INITIAL
                recent_restart_times.clear()
                persistent_crash_logged = False

    # ── consumer API for subclass encoder loops ──────────────────────────────

    def _get_pair(self, timeout: float = 1.0) -> Optional[AVPair]:
        """Pop the next pair. Returns None on timeout OR stop sentinel."""
        return self._pair_buffer.get(timeout=timeout)

    def get_pair_buffer_stats(self) -> dict:
        """Snapshot of the underlying EncodingBuffer — used by /status."""
        return self._pair_buffer.stats()

    @abstractmethod
    def _encoder_loop(self) -> None:
        """Open container, encode pairs, manage segments, flush on exit."""
        ...
