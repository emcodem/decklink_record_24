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

import io
import logging
import queue as _queue_module
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

import av
import numpy as np

from .capture.input_filter import InputVideoFilter
from .capture.decklink_com import DeckLinkCapture
from .capture_buffer import CaptureBuffer
from .config import ServiceConfig, OutputConfig
from .output.base import AudioPacket, AVPair, OutputThread, VideoFrame
from .output.encoder import EncoderOutput
from .output.output_filter import OutputVideoFilter
from .sync_log import (
    log_channel_heartbeat, log_frame_arrival_recovered, log_frame_arrival_stall,
    log_join_timeout, log_lifecycle_event, log_signal_loss,
)

logger = logging.getLogger("ffrecord.service")

DISK_CHECK_INTERVAL = 30.0     # seconds between free-space checks
DISK_PAUSE_THRESHOLD_GB = 5.0  # pause writes below this free space
DISK_RESUME_THRESHOLD_GB = 10.0

CHANNEL_HEARTBEAT_INTERVAL = 5.0
# Multiplier applied to expected frame duration; if no DeckLink callback has
# arrived in (FRAME_STALL_FACTOR × frame_duration) seconds AND the device
# reports signal_locked=True, the heartbeat fires FRAME_ARRIVAL_STALL.
FRAME_STALL_FACTOR = 3.0

# BMDFieldDominance FourCC values (DeckLink SDK uses 4-byte ASCII codes, not small integers)
_BMD_UPPER_FIELD_FIRST = 0x75707072  # 'uppr' — TFF
_BMD_LOWER_FIELD_FIRST = 0x6C6F7772  # 'lowr' — BFF

# Sentinel objects for the raw-video queue
_STOP_SENTINEL = object()

# Maps bmdMode* strings to (width, height, (fps_num, fps_den)).
# Interlaced modes report the field rate in the name but the FRAME rate here,
# because InputVideoFilter and all encoders work in frames, not fields.
_BMD_FORMAT_TABLE: dict[str, tuple[int, int, tuple[int, int]]] = {
    "bmdModeHD1080i50":   (1920, 1080, (25000, 1000)),
    "bmdModeHD1080i5994": (1920, 1080, (30000, 1001)),
    "bmdModeHD1080i6000": (1920, 1080, (30000, 1000)),
    "bmdModeHD1080p24":   (1920, 1080, (24000, 1000)),
    "bmdModeHD1080p25":   (1920, 1080, (25000, 1000)),
    "bmdModeHD1080p2997": (1920, 1080, (30000, 1001)),
    "bmdModeHD1080p30":   (1920, 1080, (30000, 1000)),
    "bmdModeHD1080p50":   (1920, 1080, (50000, 1000)),
    "bmdModeHD1080p5994": (1920, 1080, (60000, 1001)),
    "bmdModeHD1080p6000": (1920, 1080, (60000, 1000)),
    "bmdModeHD720p50":    (1280,  720, (50000, 1000)),
    "bmdModeHD720p5994":  (1280,  720, (60000, 1001)),
    "bmdModeHD720p60":    (1280,  720, (60000, 1000)),
    "bmdModeSD525i5994":  ( 720,  487, (30000, 1001)),
    "bmdModeSD625i50":    ( 720,  576, (25000, 1000)),
}


def _parse_bmd_format(fmt: str) -> tuple[int, int, tuple[int, int]]:
    entry = _BMD_FORMAT_TABLE.get(fmt)
    if entry:
        return entry
    logger.warning(
        "Unknown BMD format '%s' — defaulting to 1920x1080 @ 25fps for filter pre-warm", fmt,
    )
    return 1920, 1080, (25000, 1000)


def _is_interlaced_format(fmt: str) -> bool:
    """True iff the BMD mode name indicates interlaced scan (e.g. bmdModeHD1080i50)."""
    import re
    return bool(re.search(r"[iI]\d+$", fmt))

# Maximum raw frames to buffer before dropping; each 1080i50 frame is ~4 MB,
# so 30 frames ≈ 120 MB and absorbs any startup filter-init spike.
_MAX_RAW_QUEUE = 30

# Sentinel tuple tag used to relay format-change events through the frame queue
_FORMAT_CHANGE_TAG = "_format_change"


def _build_output(cfg: OutputConfig, channel_name: str) -> OutputThread:
    return EncoderOutput(cfg, channel_name)


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

        # hw_pts-based A/V pairing buffer. Sits between capture callbacks and
        # the per-output queues. Each completed AVPair is fan-out to every
        # enabled output via _emit_pair.
        self._capture_buffer = CaptureBuffer(
            emit_callback=self._emit_pair,
            default_audio_channels=config.capture.audio_channels,
            default_audio_sample_rate=48000,
        )

        # Off-thread frame processing: the DeckLink COM callback must return in
        # microseconds; filter-graph init and pixel-format conversion happen here
        # instead, in a dedicated thread.
        self._raw_video_queue: _queue_module.Queue = _queue_module.Queue()
        self._video_processor_thread: Optional[threading.Thread] = None

        # Heartbeat / watchdog state. _raw_video_queue_peak resets each heartbeat
        # so operators see the worst-case backpressure of the last interval, not
        # of the whole session. _frame_stall_latched prevents the watchdog from
        # logging the same stall every heartbeat — it logs once on entry and
        # once on recovery.
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._raw_video_queue_peak: int = 0
        self._frame_stall_latched: bool = False
        self._frame_stall_started_at: float = 0.0
        # Cache of the most recent format string for the heartbeat. Updated by
        # _on_format_change.
        self._current_format: Optional[str] = None

    def _prewarm_input_filter(self) -> None:
        """Build the libav filter graph before opening the DeckLink device.

        InputVideoFilter._probe_output_framerate() pushes 6 blank frames through
        the graph to determine the output framerate. This holds the GIL for ~200ms.
        If it runs during the first COM callback the DeckLink ring buffer fills to
        capacity (qdepth=11) and the driver crashes. Building the graph here —
        while capture has not started yet — burns the spike harmlessly.

        The pre-warmed filter is stored in self._video_filter so _process_video_frame
        skips re-initialization on the first real frame.
        """
        cap = self.config.capture
        if not cap.video_filter:
            logger.info("No video_filter configured — skipping filter pre-warm")
            return

        fmt = self.config.channel.expected_format
        width, height, framerate = _parse_bmd_format(fmt)
        logger.info(
            "Pre-warming filter graph ('%s') for %s (%dx%d @ %d/%d) before opening DeckLink ...",
            cap.video_filter, fmt, width, height, framerate[0], framerate[1],
        )
        try:
            self._video_filter = InputVideoFilter(
                width, height, framerate,
                spec=cap.video_filter,
                pix_fmt=cap.pix_fmt,
            )
            logger.info("Filter graph pre-warmed — GIL spike consumed before capture starts")
        except Exception as exc:
            logger.warning(
                "Filter pre-warm failed (%s) — will init lazily on first frame", exc,
            )
            self._video_filter = None

    def _prewarm_output_filters(self) -> dict[str, tuple]:
        """Pre-build each output's video filter graph before opening the DeckLink device.

        OutputVideoFilter._probe_output() pushes 8 test frames through filters like
        yadif+scale, holding the GIL for up to 2 seconds. Running the probe here —
        while no COM callbacks are active — loads the required libav codec libraries
        so the real lazy-init in the output thread is negligibly fast.

        Returns {output_name: (enc_w, enc_h, enc_framerate, enc_pix_fmt)} for each
        pre-warmed filter. The encoder pre-warm uses these for exact-parameter matching.
        """
        # The channel-level filter doesn't expose interlace info, so derive it
        # from the configured expected_format (e.g. bmdModeHD1080i50). The runtime
        # OutputVideoFilter creation in *_output.py uses pair.video.* directly.
        ch_interlaced = _is_interlaced_format(self.config.channel.expected_format)
        ch_tff = True  # DeckLink HD modes are TFF by convention

        if self._video_filter is not None:
            in_w = self._video_filter.output_width
            in_h = self._video_filter.output_height
            in_fr = self._video_filter.output_framerate
            in_fmt = self._video_filter.output_pix_fmt
        else:
            in_w, in_h, in_fr = _parse_bmd_format(self.config.channel.expected_format)
            in_fmt = "uyvy422"

        # Channel passthrough dimensions (used for outputs without their own filter).
        # Tuple extended with (interlaced, tff) so encoder pre-warm can pass them through.
        ch_dims = (in_w, in_h, in_fr, in_fmt, ch_interlaced, ch_tff)
        filter_outputs: dict[str, tuple] = {}

        for out_cfg in self.config.outputs:
            if not out_cfg.enabled:
                continue
            if not out_cfg.video_filter:
                filter_outputs[out_cfg.name] = ch_dims
                continue
            logger.info(
                "Pre-warming output filter '%s' for output '%s' ...",
                out_cfg.video_filter, out_cfg.name,
            )
            try:
                dummy = OutputVideoFilter(
                    in_w, in_h, in_fr, in_fmt, out_cfg.video_filter,
                    input_interlaced=ch_interlaced, input_top_field_first=ch_tff,
                )
                filter_outputs[out_cfg.name] = (
                    dummy.output_width, dummy.output_height,
                    dummy.output_framerate, dummy.output_pix_fmt,
                    dummy.output_interlaced, dummy.output_top_field_first,
                )
                dummy.close()
                logger.info("Output filter '%s' pre-warmed.", out_cfg.name)
            except Exception as exc:
                logger.warning(
                    "Output filter pre-warm failed for '%s' (%s) — may cause GIL spike on first frame",
                    out_cfg.name, exc,
                )
                filter_outputs[out_cfg.name] = ch_dims  # best-effort fallback

        return filter_outputs

    def start(self) -> None:
        logger.info("Service starting for channel %s", self.channel_name)

        self._outputs = [
            _build_output(cfg, self.channel_name)
            for cfg in self.config.outputs
            if cfg.enabled
        ]
        for out in self._outputs:
            out.start()

        # Pre-warm the channel filter, output filters, and encoder contexts BEFORE
        # opening the DeckLink device. Each of these holds the GIL for 100ms–2s on
        # first use (libav codec library loading, CUDA context creation for NVENC).
        self._prewarm_input_filter()
        filter_outputs = self._prewarm_output_filters()

        # For HLS outputs: open the real playlist container and prime the encoder.
        # The pre-warmed container is kept alive and adopted by _encoder_loop() on the
        # first pair — the NVENC session is never destroyed between pre-warm and use.
        # OutputThread.prewarm_codec() is a no-op by default; EncoderOutput overrides.
        for out in self._outputs:
            dims = filter_outputs.get(getattr(out, "cfg", None) and out.cfg.name)
            if dims is not None:
                enc_w, enc_h, enc_fr, enc_pix_fmt = dims[0], dims[1], dims[2], dims[3]
                out.prewarm_codec(enc_w, enc_h, enc_fr, enc_pix_fmt)

        # Start the frame processor before opening the DeckLink device so the
        # thread is ready to drain frames the moment the first callback fires.
        self._video_processor_thread = threading.Thread(
            target=self._video_processor_loop,
            name="video-processor",
            daemon=True,
        )
        self._video_processor_thread.start()

        self._capture = DeckLinkCapture(
            device_index=self.config.channel.decklink_device_index,
            audio_channels=self.config.capture.audio_channels,
            format_change_callback=self._on_format_change,
            fallback_mode=self.config.channel.expected_format,
        )
        self._capture.start(
            frame_callback=self._on_video_frame,
            audio_callback=self._on_audio_packet,
        )

        disk_thread = threading.Thread(target=self._disk_monitor_loop, name="disk-monitor", daemon=True)
        disk_thread.start()

        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, name="channel-heartbeat", daemon=True,
        )
        self._heartbeat_thread.start()

        logger.info("Service started (%d outputs)", len(self._outputs))

    def stop(self) -> None:
        logger.info("Service stopping...")
        self._stop_event.set()

        # Stop the DeckLink device first so no new frames enter the queue.
        if self._capture:
            self._capture.stop()

        # Drain remaining queued frames, then shut the processor thread down.
        if self._video_processor_thread is not None:
            self._raw_video_queue.put(_STOP_SENTINEL)
            self._video_processor_thread.join(timeout=10)
            if self._video_processor_thread.is_alive():
                log_join_timeout("video-processor", 10.0)
            self._video_processor_thread = None

        for out in self._outputs:
            out.stop()

        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=CHANNEL_HEARTBEAT_INTERVAL + 1.0)
            if self._heartbeat_thread.is_alive():
                log_join_timeout("channel-heartbeat", CHANNEL_HEARTBEAT_INTERVAL + 1.0)
            self._heartbeat_thread = None

        if self._video_filter:
            self._video_filter.close()

        logger.info("Service stopped")

    # ── capture callbacks (run on DeckLink COM thread — must be non-blocking) ──

    def _on_video_frame(self, frame_bytes: bytes, width: int, height: int,
                        pixel_format: int, framerate: tuple, flags: int, row_bytes: int,
                        stream_time: int, timescale: int, stream_time_valid: bool,
                        hw_ref_time: int, hw_ref_valid: bool, tc_str: str) -> None:
        """Queue raw frame for off-thread processing. Returns immediately."""
        if self._global_paused or self._disk_paused:
            return
        qsize_now = self._raw_video_queue.qsize()
        if qsize_now >= _MAX_RAW_QUEUE:
            logger.warning(
                "Video processor queue full (%d frames) — dropping frame stream_time=%d",
                qsize_now, stream_time,
            )
            return
        self._raw_video_queue.put_nowait((
            frame_bytes, width, height, pixel_format, framerate,
            flags, row_bytes, stream_time, timescale, stream_time_valid,
            hw_ref_time, hw_ref_valid, tc_str,
        ))
        # +1 to count the frame we just enqueued before the processor pops it.
        if qsize_now + 1 > self._raw_video_queue_peak:
            self._raw_video_queue_peak = qsize_now + 1

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
        self._capture_buffer.push_audio(pkt)

    def _on_format_change(self, new_format: str) -> None:
        """COM callback — log immediately, then queue a sentinel so the processor
        handles filter/buffer reset in frame order (after all pre-change frames)."""
        log_lifecycle_event(
            "FORMAT_CHANGE_BEGIN",
            old=self._last_format, new=new_format,
        )
        logger.warning(
            "Format change detected: %s → %s. Treating as signal-loss: resetting filter graph.",
            self._last_format, new_format,
        )
        log_signal_loss(f"format_change old={self._last_format} new={new_format}")
        self._last_format = new_format
        self._current_format = new_format
        self._raw_video_queue.put_nowait((_FORMAT_CHANGE_TAG, new_format))

    # ── off-thread frame processing ──────────────────────────────────────────

    def _video_processor_loop(self) -> None:
        """Drain the raw-video queue: run filter graph and push to capture buffer."""
        while True:
            try:
                item = self._raw_video_queue.get(timeout=0.5)
            except _queue_module.Empty:
                if self._stop_event.is_set():
                    break
                continue

            if item is _STOP_SENTINEL:
                break

            if isinstance(item, tuple) and len(item) == 2 and item[0] == _FORMAT_CHANGE_TAG:
                self._do_format_reset(item[1])
                continue

            try:
                self._process_video_frame(*item)
            except Exception as e:
                logger.error("Error processing video frame: %s", e, exc_info=True)

    def _do_format_reset(self, new_format: str) -> None:
        """Called from the processor thread after all pre-change frames are drained."""
        if self._video_filter:
            self._video_filter.close()
            self._video_filter = None
        self._capture_buffer.reset()
        for out in self._outputs:
            out.notify_format_change()

    def _process_video_frame(self, frame_bytes: bytes, width: int, height: int,
                             pixel_format: int, framerate: tuple, flags: int, row_bytes: int,
                             stream_time: int, timescale: int, stream_time_valid: bool,
                             hw_ref_time: int, hw_ref_valid: bool, tc_str: str) -> None:
        """Filter, convert, and push one frame to the capture buffer."""
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

        # The filter (or passthrough) yields its output frames; its `output_*` fields
        # report what the encoder downstream should expect (size, framerate, pix_fmt).
        vf_ = self._video_filter

        # Derive interlace flags from DeckLink's GetFieldDominance() first.
        # BMDFieldDominance: 2 = bmdUpperFieldFirst (TFF), 1 = bmdLowerFieldFirst (BFF)
        dom = self._capture.field_dominance if self._capture else None
        if dom == _BMD_UPPER_FIELD_FIRST:
            decklink_interlaced, decklink_tff = True, True
        elif dom == _BMD_LOWER_FIELD_FIRST:
            decklink_interlaced, decklink_tff = True, False
        else:
            decklink_interlaced, decklink_tff = False, False

        if self._frame_count == 0:
            logger.info(
                "Interlace source — GetFieldDominance=%s → interlaced=%s tff=%s",
                dom, decklink_interlaced, decklink_tff,
            )

        # Seed the filter so format-only filters (e.g. format=yuv420p) propagate the
        # DeckLink metadata rather than defaulting to progressive. Filters that
        # explicitly change field order (setfield, yadif, bwdif) will override it.
        vf_.input_interlaced_frame = decklink_interlaced
        vf_.input_top_field_first = decklink_tff

        frames_out = list(vf_.process(frame_bytes, width, height, row_bytes))
        out_w = vf_.output_width
        out_h = vf_.output_height
        out_fr = vf_.output_framerate
        out_fmt = vf_.output_pix_fmt

        # After the filter runs, output_interlaced_frame/top_field_first reflect what
        # the filter reported (with the seeded DeckLink values as fallback). Passthrough
        # leaves them None, so we use DeckLink directly.
        if vf_.output_interlaced_frame is not None:
            out_interlaced = vf_.output_interlaced_frame
            out_tff = vf_.output_top_field_first
        else:
            out_interlaced, out_tff = decklink_interlaced, decklink_tff

        if self._frame_count == 0:
            logger.info(
                "Interlace result — filter_out: interlaced=%s tff=%s → VideoFrame: interlaced=%s tff=%s",
                vf_.output_interlaced_frame, vf_.output_top_field_first,
                out_interlaced, out_tff,
            )

        # When the filter doubles framerate (e.g. yadif=mode=1 turning 25i into
        # 50p), one DeckLink callback yields N output frames. Each must get a
        # distinct hw_pts spaced by the OUTPUT frame duration — otherwise the
        # CaptureBuffer pairing logic sees multiple frames at the same hw_pts
        # and silences all but the first.
        out_fr_num, out_fr_den = out_fr
        if out_fr_num > 0:
            output_frame_duration = timescale * out_fr_den // out_fr_num
        else:
            # Should not happen — the InputVideoFilter always reports a sane
            # framerate after configure(). If we reach this, the filter graph
            # is misconfigured or the framerate probe failed.
            logger.error(
                "Invalid output framerate from InputVideoFilter: %s — "
                "falling back to 50fps spacing. This indicates a filter "
                "configuration bug; A/V pairing will likely be wrong.",
                out_fr,
            )
            output_frame_duration = timescale // 50

        for idx, av_frame_out in enumerate(frames_out):
            # InputVideoFilter now yields av.VideoFrame (with interlaced metadata
            # preserved from the channel-level setfield/yadif). We extract numpy
            # for paths that need it and also keep the AVFrame for the encoder.
            arr = av_frame_out.to_ndarray(format=out_fmt)
            video_frame = VideoFrame(
                data=arr,
                fmt=out_fmt,
                width=out_w,
                height=out_h,
                framerate=out_fr,
                hw_pts=stream_time + idx * output_frame_duration,
                hw_pts_rate=timescale,
                hw_pts_valid=stream_time_valid,
                timecode=tc_str,
                interlaced_frame=out_interlaced,
                top_field_first=out_tff,
                av_frame=av_frame_out,
            )
            # Feed the pairing buffer instead of pushing directly to outputs.
            # CaptureBuffer will call back via _emit_pair when a complete
            # AVPair is ready.
            self._capture_buffer.push_video(video_frame)

        self._frame_count += 1

    def _emit_pair(self, pair: AVPair) -> None:
        """Fan-out a completed AVPair to every enabled output.

        Called synchronously by CaptureBuffer from inside its lock — must be
        cheap. push_pair() on each output is non-blocking (drops on full).
        """
        for out in self._outputs:
            out.push_pair(pair)

    # ── disk monitoring ──────────────────────────────────────────────────────

    def _disk_monitor_loop(self) -> None:
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=DISK_CHECK_INTERVAL)
            if self._stop_event.is_set():
                break
            self._check_disk()
            self._check_signal_lock()

    # ── heartbeat / frame-arrival watchdog ───────────────────────────────────

    def _expected_frame_duration_s(self) -> float:
        """Frame duration in seconds, derived from the current capture format.

        Falls back to channel.expected_format when no format-change has arrived
        yet (so the watchdog has a sensible threshold during startup).
        """
        fmt = self._current_format or self.config.channel.expected_format
        try:
            _, _, (num, den) = _parse_bmd_format(fmt)
            if num > 0 and den > 0:
                return den / num
        except Exception:
            pass
        return 1.0 / 25.0  # last-resort default

    def _heartbeat_loop(self) -> None:
        """Emit a structured health snapshot every CHANNEL_HEARTBEAT_INTERVAL
        seconds and run the frame-arrival watchdog.

        Single thread serves both jobs to keep the side-channel cost low — one
        sleep, one read of shared monotonic timestamps, no locks.
        """
        while not self._stop_event.is_set():
            if self._stop_event.wait(timeout=CHANNEL_HEARTBEAT_INTERVAL):
                break

            now = time.monotonic()
            cap_stats = self._capture_buffer.get_stats()
            last_cb = self._capture.last_callback_monotonic if self._capture else 0.0
            callback_age = (now - last_cb) if last_cb > 0 else None
            signal_locked = None
            if self._capture is not None:
                try:
                    signal_locked = self._capture.get_signal_locked()
                except Exception:
                    signal_locked = None

            # Watchdog: frames stopped arriving despite a locked signal.
            frame_duration = self._expected_frame_duration_s()
            stall_threshold = FRAME_STALL_FACTOR * frame_duration
            if (
                callback_age is not None
                and callback_age > stall_threshold
                and signal_locked is True
            ):
                if not self._frame_stall_latched:
                    self._frame_stall_latched = True
                    self._frame_stall_started_at = last_cb
                    log_frame_arrival_stall(callback_age, frame_duration)
            elif self._frame_stall_latched and callback_age is not None and callback_age <= stall_threshold:
                stall_duration = (last_cb - self._frame_stall_started_at)
                self._frame_stall_latched = False
                log_frame_arrival_recovered(stall_duration)

            queue_peak = self._raw_video_queue_peak
            self._raw_video_queue_peak = self._raw_video_queue.qsize()
            audio_overflow = (
                self._capture.audio_overflow_dropped if self._capture else 0
            )

            log_channel_heartbeat({
                "pending_v": cap_stats.pending_video,
                "pending_a": cap_stats.pending_audio,
                "buf_bytes": cap_stats.buffered_video_bytes,
                "emitted": cap_stats.emitted_total,
                "stale_drops": cap_stats.stale_audio_drops,
                "rawq": self._raw_video_queue.qsize(),
                "rawq_peak": queue_peak,
                "cb_age_s": f"{callback_age:.2f}" if callback_age is not None else "n/a",
                "signal_locked": signal_locked,
                "format": self._current_format or "unknown",
                "audio_overflow": audio_overflow,
                "disk_paused": self._disk_paused,
                "global_paused": self._global_paused,
                "stall_latched": self._frame_stall_latched,
            })

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
                log_lifecycle_event(
                    "DISK_PAUSE_ENGAGED",
                    free_gb=f"{free_gb:.1f}",
                    threshold_gb=DISK_PAUSE_THRESHOLD_GB,
                )
                logger.error(
                    "[disk] Free space %.1f GB below pause threshold %.1f GB — pausing all writes",
                    free_gb, DISK_PAUSE_THRESHOLD_GB,
                )
            elif self._disk_paused and free_gb >= DISK_RESUME_THRESHOLD_GB:
                self._disk_paused = False
                log_lifecycle_event(
                    "DISK_PAUSE_RELEASED",
                    free_gb=f"{free_gb:.1f}",
                    threshold_gb=DISK_RESUME_THRESHOLD_GB,
                )
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
        log_lifecycle_event(
            "GLOBAL_PAUSE_ENGAGED" if paused else "GLOBAL_PAUSE_RELEASED",
        )
        logger.info("Global pause: %s", paused)

    def set_output_enabled(self, name: str, enabled: bool) -> bool:
        for out in self._outputs:
            if out.name == name:
                out.set_enabled(enabled)
                log_lifecycle_event(
                    "OUTPUT_ENABLED" if enabled else "OUTPUT_DISABLED",
                    name=name,
                )
                return True
        return False

    def get_status(self) -> dict:
        usage = None
        try:
            u = shutil.disk_usage(self.config.logging.dir)
            usage = {"free_gb": round(u.free / 1024**3, 2), "total_gb": round(u.total / 1024**3, 2)}
        except Exception:
            pass

        # CaptureBuffer state — single-spot pairing diagnostics for the operator.
        cb_stats = self._capture_buffer.get_stats()
        hw_pts_rate = 10_000_000

        def _ticks_to_us(ticks: int) -> int:
            return ticks * 1_000_000 // hw_pts_rate

        pairing = {
            "pending_video_frames": cb_stats.pending_video,
            "pending_audio_packets": cb_stats.pending_audio,
            "buffered_video_bytes": cb_stats.buffered_video_bytes,
            "buffered_video_mb": round(cb_stats.buffered_video_bytes / (1024 * 1024), 2),
            "emitted_pairs_total": cb_stats.emitted_total,
            "stale_audio_drops_total": cb_stats.stale_audio_drops,
            "audio_gaps_total": cb_stats.audio_gaps,
            "catchup_silence_frames_total": cb_stats.catchup_silence_frames,
            "forced_silence_frames_total": cb_stats.forced_silence_frames,
            "last_pair_delta_us": _ticks_to_us(cb_stats.last_pair_delta_ticks),
            "jitter_window_min_us": _ticks_to_us(cb_stats.jitter_min_ticks),
            "jitter_window_max_us": _ticks_to_us(cb_stats.jitter_max_ticks),
        }

        return {
            "channel": self.channel_name,
            "capture": self._capture.get_status() if self._capture else {},
            "global_paused": self._global_paused,
            "disk_paused": self._disk_paused,
            "frames_captured": self._frame_count,
            "raw_video_queue_depth": self._raw_video_queue.qsize(),
            "disk": usage,
            "pairing": pairing,
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
                    "synthesized_audio_frames": out.stats.synthesized_audio_frames,
                    "pair_queue": out.get_pair_buffer_stats(),
                }
                for out in self._outputs
            ],
        }

