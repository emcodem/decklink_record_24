"""DeckLink COM capture wrapper — extended for deep A/V-sync diagnostics.

Extends ffcapture's decklink_com.py with:
  - GetHardwareReferenceTimestamp per frame
  - IDeckLinkTimecode / SMPTE timecode extraction
  - Input queue depth via GetAvailableVideoFrameCount
  - bmdFrameHasNoInputSource flag for dropped/no-signal frame detection
  - VideoInputFormatChanged wired to a user-supplied format_change_callback
    so Service can treat it as a signal-loss event.

Capture-only: output/playout code from ffcapture is not included.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from typing import Callable, Optional

import numpy as np

from ..sync_log import (
    log_audio_overflow_summary, log_audio_packet, log_audio_pts_overlap,
    log_audio_sample_gap, log_av_lag, log_missed_frames, log_psf_frame,
    log_signal_loss, log_signal_return, log_video_frame,
)

logger = logging.getLogger("ffrecord.capture")

TIMESCALE = 10_000_000   # DeckLink native 10 MHz hardware clock

# Number of initial frames/packets after each format change that are logged
# verbosely at INFO level via sync_log. After that, sync_log uses DEBUG.
_DIAG_BURST = 20

try:
    from comtypes.client import GetModule
    _dll_path = r"C:\Program Files\Blackmagic Design\Blackmagic Desktop Video\DeckLinkAPI64.dll"
    _dl = GetModule(_dll_path)
    HAS_COMTYPES = True
except Exception as _e:
    logger.error("Failed to load DeckLink type library: %s", _e)
    HAS_COMTYPES = False
    _dl = None


def _tc_string(tc_obj) -> str:
    """Extract HH:MM:SS:FF string from an IDeckLinkTimecode COM object."""
    try:
        h = ctypes.c_uint8(0)
        m = ctypes.c_uint8(0)
        s = ctypes.c_uint8(0)
        f = ctypes.c_uint8(0)
        tc_obj.GetComponents(
            ctypes.byref(h), ctypes.byref(m),
            ctypes.byref(s), ctypes.byref(f)
        )
        return f"{h.value:02d}:{m.value:02d}:{s.value:02d}:{f.value:02d}"
    except Exception:
        return ""


def _create_input_callback(
    frame_callback: Callable,
    audio_callback: Callable,
    audio_channels: int,
    decklink_input,
    format_change_callback: Optional[Callable] = None,
    shared_framerate: Optional[list] = None,
    shared_field_dominance: Optional[list] = None,
    shared_audio_overflow_dropped: Optional[list] = None,
    shared_last_callback_monotonic: Optional[list] = None,
):
    """Return a COM-backed IDeckLinkInputCallback."""
    from comtypes import COMObject

    _framerate = shared_framerate if shared_framerate is not None else [None]
    _field_dominance = shared_field_dominance if shared_field_dominance is not None else [None]
    _current_mode = [None]
    _video_hw_pts_fail = [0]
    _audio_hw_pts_fail = [0]
    _hw_ref_fail = [0]
    _diag_frames = [0]
    _diag_audio = [0]
    _frame_n = [0]
    _audio_n = [0]
    _last_stream_time = [None]   # stream_time of the most-recently-delivered frame
    _last_frame_duration = [0]   # duration (ticks) of that frame, used to predict next
    _decklink_qdepth_high = [False]   # True while driver queue depth >= threshold
    # Startup-only audio-overflow safety net. The encoder thread's first-frame
    # work on full-HD interlaced video stalls the COM thread for ~2 s, during
    # which DeckLink's driver-side audio buffer (hard-capped at 48000 samples
    # = 1 s by Blackmagic) overflows. When the COM thread resumes and we read
    # the now-stale audio packet via audioPacket.GetBytes(), the buffer pointer
    # is invalidated and we get an 0xC0000005 access violation.
    #
    # Window: first STARTUP_CALLBACK_WINDOW video callbacks. After that, if
    # audio_qdepth ever spikes high, something else is wrong (disk stall, GC,
    # downstream encoder stuck) and we WANT to see the warning / crash to know
    # about it — silent dropping would mask real bugs.
    #
    # During the window: when audio_qdepth >= STARTUP_AUDIO_LATCH_QDEPTH, latch
    # a "skip audio reads" flag. Stays latched until audio_qdepth drains below
    # STARTUP_AUDIO_UNLATCH_QDEPTH (driver fully recovered). While latched, the
    # audio packet block is skipped entirely — no GetBytes() on possibly-stale
    # buffers.
    _startup_callbacks_remaining = [100]  # first ~4 s of callbacks at 25 fps
    _audio_overflow_latched = [False]
    # Bound to the DeckLinkCapture-level shared counter so /status can read it.
    _audio_overflow_dropped = (
        shared_audio_overflow_dropped if shared_audio_overflow_dropped is not None else [0]
    )
    _startup_summary_logged = [False]
    # Monotonic timestamp of the most recent callback — shared with the Service
    # heartbeat for the frame-arrival watchdog (L1/L2).
    _last_callback_monotonic = (
        shared_last_callback_monotonic if shared_last_callback_monotonic is not None else [0.0]
    )
    _last_audio_pts = [None]     # pts of most recently delivered audio packet
    _last_audio_samples = [0]    # sample count of that packet (for gap computation)
    _audio_size_last = [0]       # last seen sample_count (to detect size changes)
    _missing_audio_pkts = [0]    # callbacks where audioPacket was absent

    class _CB(COMObject):
        _com_interfaces_ = [_dl.IDeckLinkInputCallback]

        def VideoInputFormatChanged(self, notificationEvents, newDisplayMode, detectedSignalFlags):
            try:
                try:
                    mode_constant = newDisplayMode.GetDisplayMode()
                except Exception:
                    mode_constant = None

                if mode_constant is not None and mode_constant == _current_mode[0]:
                    return 0

                try:
                    mode_name = newDisplayMode.GetName()
                except Exception:
                    mode_name = "unknown"
                try:
                    width = newDisplayMode.GetWidth()
                    height = newDisplayMode.GetHeight()
                except Exception:
                    width = height = 0

                fps_str = "unknown"
                try:
                    raw = newDisplayMode.GetFrameRate()
                    fps_num, fps_den = int(raw[1]), int(raw[0])
                    _framerate[0] = (fps_num, fps_den)
                    fps_str = f"{fps_num}/{fps_den}"
                except Exception as e:
                    logger.error("GetFrameRate failed: %s", e)

                try:
                    _field_dominance[0] = int(newDisplayMode.GetFieldDominance())
                except Exception as e:
                    logger.warning("GetFieldDominance failed: %s", e)

                progressive = bool(detectedSignalFlags & getattr(_dl, 'bmdDetectedVideoInputProgressive', 0x08))
                scan = "progressive" if progressive else "interlaced"
                fmt_str = f"{mode_name} {width}x{height} {fps_str} {scan}"

                logger.info("=== DeckLink Signal Detected ===")
                logger.info("  Mode      : %s", fmt_str)
                logger.info("  Flags     : 0x%08x", detectedSignalFlags)
                logger.info("================================")

                _current_mode[0] = mode_constant
                _diag_frames[0] = 0
                _diag_audio[0] = 0
                _last_stream_time[0] = None
                _last_frame_duration[0] = 0
                _decklink_qdepth_high[0] = False
                _last_audio_pts[0] = None
                _last_audio_samples[0] = 0
                _audio_size_last[0] = 0

                if decklink_input is not None and mode_constant is not None:
                    try:
                        decklink_input.PauseStreams()
                        decklink_input.EnableVideoInput(
                            mode_constant,
                            _dl.bmdFormat8BitYUV,
                            _dl.bmdVideoInputEnableFormatDetection,
                        )
                        decklink_input.FlushStreams()
                        decklink_input.StartStreams()
                    except Exception as e:
                        logger.error("Failed to restart streams on format change: %s", e, exc_info=True)

                if format_change_callback:
                    try:
                        format_change_callback(fmt_str)
                    except Exception as e:
                        logger.error("format_change_callback error: %s", e)

                log_signal_return(fmt_str)

            except Exception as e:
                logger.error("Error in VideoInputFormatChanged: %s", e, exc_info=True)
            return 0

        def VideoInputFrameArrived(self, videoFrame, audioPacket):
            try:
                # Record arrival time for the Service-level frame-arrival watchdog.
                # Updated at the top of every callback even when we bail out early,
                # so a latched startup overflow does not look like a frame stall.
                _last_callback_monotonic[0] = time.monotonic()

                if _framerate[0] is None:
                    return 0

                # ── Startup-only audio-overflow safety net ───────────────────
                # When DeckLink's driver-side audio buffer overflows (hard-capped
                # at 48000 samples = 1 s), the videoFrame AND audioPacket COM
                # objects passed to this callback can have stale/invalidated
                # internal state. Touching ANY method on them — including
                # videoFrame.GetWidth() — can dereference a freed buffer and
                # crash with 0xC0000005.
                #
                # Strategy: at the top of every callback during the startup
                # window, sample audio_qdepth (a safe API on decklink_input
                # itself, not on the per-frame objects). If we're either above
                # the latch threshold or already latched, return 0 immediately
                # without touching videoFrame or audioPacket.
                #
                # The window is bounded by _startup_callbacks_remaining (~4 s
                # at 25 fps). After that, we always fall through to normal
                # processing — a mid-stream overflow surfaces as the normal
                # warning / crash so real bugs aren't masked.
                if _startup_callbacks_remaining[0] > 0:
                    try:
                        _startup_audio_qdepth = decklink_input.GetAvailableAudioSampleFrameCount()
                    except Exception:
                        _startup_audio_qdepth = 0
                    _STARTUP_AUDIO_LATCH_QDEPTH = 30000     # ~625 ms
                    _STARTUP_AUDIO_UNLATCH_QDEPTH = 1920    # one frame worth
                    if not _audio_overflow_latched[0] and _startup_audio_qdepth >= _STARTUP_AUDIO_LATCH_QDEPTH:
                        _audio_overflow_latched[0] = True
                        logger.warning(
                            "[sync] AUDIO_OVERFLOW_LATCHED audio_qdepth=%d — "
                            "bailing out of callback (stale COM objects after driver overflow "
                            "would crash with 0xC0000005). Will resume when buffer drains.",
                            _startup_audio_qdepth,
                        )
                    elif _audio_overflow_latched[0] and _startup_audio_qdepth <= _STARTUP_AUDIO_UNLATCH_QDEPTH:
                        _audio_overflow_latched[0] = False
                        logger.info(
                            "[sync] AUDIO_OVERFLOW_RECOVERED audio_qdepth=%d "
                            "(skipped %d callbacks during startup recovery)",
                            _startup_audio_qdepth, _audio_overflow_dropped[0],
                        )
                    # Always decrement the window counter so it expires even
                    # while latched (we don't want a permanent latch).
                    _startup_callbacks_remaining[0] -= 1
                    if _audio_overflow_latched[0]:
                        _audio_overflow_dropped[0] += 1
                        return 0
                else:
                    # Window has just expired. Log a summary line exactly once so
                    # operators see the dropped-callback count even when it's 0 —
                    # a clean "0 callbacks skipped" line confirms a healthy
                    # startup, distinguishing "no problem" from "no log at all".
                    if not _startup_summary_logged[0]:
                        _startup_summary_logged[0] = True
                        log_audio_overflow_summary(_audio_overflow_dropped[0])
                    if _audio_overflow_latched[0]:
                        # Startup window expired while latched — clear it and let
                        # any subsequent overflow surface normally (we want to know
                        # about mid-stream stalls; silent dropping would mask bugs).
                        _audio_overflow_latched[0] = False
                        logger.warning(
                            "[sync] AUDIO_OVERFLOW_LATCH_EXPIRED startup window ended "
                            "with %d callbacks skipped. Subsequent overflows surface normally.",
                            _audio_overflow_dropped[0],
                        )

                if videoFrame and frame_callback:
                    try:
                        width = videoFrame.GetWidth()
                        height = videoFrame.GetHeight()
                        row_bytes = videoFrame.GetRowBytes()
                        pixel_format = videoFrame.GetPixelFormat()
                        flags = videoFrame.GetFlags()

                        # Detect no-signal frames
                        no_signal = bool(flags & getattr(_dl, 'bmdFrameHasNoInputSource',
                                                         from_comtypes_or_default('bmdFrameHasNoInputSource', 1 << 5)))
                        if no_signal:
                            log_signal_loss("bmdFrameHasNoInputSource mitigation=frame_passed_flagged")

                        # Detect Progressive Segmented Frame (PsF) — looks interlaced at transport level but is progressive
                        if flags & (1 << 30):  # bmdFrameCapturedAsPsF
                            log_psf_frame()

                        # Stream time (same clock domain as audio)
                        video_stream_time = 0
                        video_stream_duration = 0
                        stream_time_valid = False
                        try:
                            raw = videoFrame.GetStreamTime(TIMESCALE)
                            video_stream_time = int(raw[0])
                            video_stream_duration = int(raw[1])
                            stream_time_valid = True
                        except Exception as e:
                            _video_hw_pts_fail[0] += 1
                            _log_graduated("ffrecord.capture", _video_hw_pts_fail[0],
                                           "GetStreamTime failed", e)

                        # Detect gaps in DeckLink frame delivery
                        if (stream_time_valid
                                and _last_stream_time[0] is not None
                                and _last_frame_duration[0] > 0):
                            expected_pts = _last_stream_time[0] + _last_frame_duration[0]
                            gap_ticks = video_stream_time - expected_pts
                            if gap_ticks > _last_frame_duration[0] // 2:
                                gap_frames = max(1, round(gap_ticks / _last_frame_duration[0]))
                                log_missed_frames(
                                    gap_frames, expected_pts, video_stream_time,
                                    "pts_gap_tolerated",
                                )

                        # Hardware reference timestamp (independent wall clock)
                        hw_ref_time = 0
                        hw_ref_time_in_frame = 0  # position within frame; useful for sub-frame jitter analysis
                        hw_ref_valid = False
                        try:
                            raw_hw = videoFrame.GetHardwareReferenceTimestamp(TIMESCALE)
                            hw_ref_time = int(raw_hw[0])
                            hw_ref_time_in_frame = int(raw_hw[1])
                            hw_ref_valid = True
                        except Exception as e:
                            _hw_ref_fail[0] += 1
                            if _hw_ref_fail[0] == 1:
                                logger.warning("GetHardwareReferenceTimestamp not available: %s", e)

                        # SMPTE timecode (RP188)
                        tc_str = ""
                        try:
                            tc_format = getattr(_dl, 'bmdTimecodeRP188Any',
                                                from_comtypes_or_default('bmdTimecodeRP188Any', 0x52503138))
                            tc_obj_ptr = ctypes.c_void_p()
                            hr = videoFrame.GetTimecode(tc_format, ctypes.byref(tc_obj_ptr))
                            if hr == 0 and tc_obj_ptr.value:
                                # tc_obj_ptr is a raw COM pointer; use QueryInterface on it
                                # For simplicity, try to call GetComponents via the module's interface
                                tc_str = _tc_string(tc_obj_ptr)
                        except Exception:
                            pass

                        # Input queue depths (video frames and audio samples buffered in driver)
                        queue_depth = 0
                        try:
                            queue_depth = decklink_input.GetAvailableVideoFrameCount()
                        except Exception:
                            pass

                        # Warn when the DeckLink driver's internal ring buffer is filling up.
                        # A depth >= 3 means the callback thread is slower than frame arrival;
                        # the driver will start dropping frames at its ring buffer limit.
                        _DECKLINK_QDEPTH_THRESHOLD = 3
                        qdepth_high = queue_depth >= _DECKLINK_QDEPTH_THRESHOLD
                        if qdepth_high and not _decklink_qdepth_high[0]:
                            logger.warning(
                                "[sync] DECKLINK_BUFFER_HIGH qdepth=%d — callback thread falling behind",
                                queue_depth,
                            )
                            _decklink_qdepth_high[0] = True
                        elif not qdepth_high and _decklink_qdepth_high[0]:
                            logger.info(
                                "[sync] DECKLINK_BUFFER_RECOVERED qdepth=%d", queue_depth,
                            )
                            _decklink_qdepth_high[0] = False

                        audio_qdepth = 0
                        try:
                            audio_qdepth = decklink_input.GetAvailableAudioSampleFrameCount()
                            if audio_qdepth > 4800:  # > 100ms at 48 kHz
                                logger.warning(
                                    "[sync] AUDIO_QUEUE_DEPTH qdepth=%d (>100ms at 48kHz)", audio_qdepth
                                )
                        except Exception:
                            pass

                        # Sync log (first DIAG_BURST after each format change at INFO, then DEBUG)
                        _frame_n[0] += 1
                        if _diag_frames[0] < _DIAG_BURST:
                            _diag_frames[0] += 1
                            log_video_frame(
                                _frame_n[0], video_stream_time, hw_ref_time, hw_ref_valid,
                                hw_ref_time_in_frame, tc_str, TIMESCALE, flags,
                                queue_depth, audio_qdepth,
                            )

                        # Drop the frame without touching the buffer when the DeckLink ring
                        # buffer is critically full.  Accessing the frame data while the
                        # driver is overflowing causes a native crash.  This can happen
                        # during startup while the frame-processor thread holds the GIL
                        # initialising its libav filter graph.  Dropping a few frames here
                        # is far better than crashing.
                        _DECKLINK_CRITICAL_QDEPTH = 8
                        if queue_depth >= _DECKLINK_CRITICAL_QDEPTH:
                            logger.warning(
                                "[sync] FRAME_DROPPED qdepth=%d (>=%d) — skipping frame to drain buffer",
                                queue_depth, _DECKLINK_CRITICAL_QDEPTH,
                            )
                        else:
                            # Frame buffer.  StartAccess MUST be paired with EndAccess via
                            # try/finally — otherwise an exception in frame_callback or the
                            # ctypes.from_address path leaks the COM buffer lock and the
                            # driver eventually starves.
                            video_buffer = videoFrame.QueryInterface(_dl.IDeckLinkVideoBuffer)
                            video_buffer.StartAccess(_dl.bmdBufferAccessRead)
                            try:
                                buffer_ptr = video_buffer.GetBytes()

                                if buffer_ptr:
                                    data_size = height * row_bytes
                                    frame_data = (ctypes.c_uint8 * data_size).from_address(buffer_ptr)
                                    frame_bytes = bytes(frame_data)
                                    frame_callback(
                                        frame_bytes, width, height, pixel_format, _framerate[0],
                                        flags, row_bytes,
                                        video_stream_time, TIMESCALE, stream_time_valid,
                                        hw_ref_time, hw_ref_valid, tc_str,
                                    )
                            finally:
                                try:
                                    video_buffer.EndAccess(_dl.bmdBufferAccessRead)
                                except Exception as end_e:
                                    logger.warning(
                                        "EndAccess failed on frame %d: %s",
                                        _frame_n[0], end_e,
                                    )

                        if stream_time_valid and video_stream_duration > 0:
                            _last_stream_time[0] = video_stream_time
                            _last_frame_duration[0] = video_stream_duration

                    except Exception as e:
                        logger.error("Error processing video frame: %s", e, exc_info=True)

                if audioPacket and audio_callback:
                    try:
                        sample_count = audioPacket.GetSampleFrameCount()
                        buffer_ptr = audioPacket.GetBytes()

                        audio_pts = 0
                        audio_pts_valid = False
                        hw_ref_audio = 0
                        hw_ref_audio_valid = False
                        try:
                            raw = audioPacket.GetPacketTime(TIMESCALE)
                            audio_pts = int(raw[0]) if isinstance(raw, (tuple, list)) else int(raw)
                            audio_pts_valid = True
                        except Exception as e:
                            _audio_hw_pts_fail[0] += 1
                            _log_graduated("ffrecord.capture", _audio_hw_pts_fail[0],
                                           "GetPacketTime failed", e)

                        _audio_n[0] += 1

                        # Audio pts continuity — detect gaps (missing samples) and overlaps.
                        # Tolerance is 2 samples to absorb integer-division rounding in our
                        # expected_pts calculation (DeckLink hardware pts itself is accurate).
                        if audio_pts_valid:
                            if _last_audio_pts[0] is not None and _last_audio_samples[0] > 0:
                                expected_pts = (
                                    _last_audio_pts[0]
                                    + _last_audio_samples[0] * TIMESCALE // 48000
                                )
                                gap_ticks = audio_pts - expected_pts
                                tol_ticks = 2 * TIMESCALE // 48000
                                if gap_ticks > tol_ticks:
                                    gap_samples = round(gap_ticks * 48000 / TIMESCALE)
                                    log_audio_sample_gap(
                                        _audio_n[0], gap_samples, gap_ticks,
                                        _last_audio_pts[0], audio_pts, TIMESCALE,
                                    )
                                elif gap_ticks < -tol_ticks:
                                    log_audio_pts_overlap(
                                        _audio_n[0], gap_ticks,
                                        _last_audio_pts[0], audio_pts, TIMESCALE,
                                    )
                            _last_audio_pts[0] = audio_pts
                            _last_audio_samples[0] = sample_count

                        # Log packet size changes after the initial burst to catch
                        # irregular delivery (e.g. size drift at fractional framerates).
                        if (_audio_size_last[0] != 0
                                and sample_count != _audio_size_last[0]
                                and _diag_audio[0] >= _DIAG_BURST):
                            logger.info(
                                "[sync] AUDIO_SIZE_CHANGE pkt_n=%d old=%d new=%d",
                                _audio_n[0], _audio_size_last[0], sample_count,
                            )
                        _audio_size_last[0] = sample_count

                        if _diag_audio[0] < _DIAG_BURST:
                            _diag_audio[0] += 1
                            log_audio_packet(
                                _audio_n[0], audio_pts, hw_ref_audio, hw_ref_audio_valid,
                                TIMESCALE, sample_count,
                            )
                            if audio_pts_valid and _last_stream_time[0] is not None:
                                log_av_lag(
                                    _audio_n[0], _last_stream_time[0], audio_pts, TIMESCALE,
                                )

                        if buffer_ptr and sample_count > 0:
                            data_size = sample_count * audio_channels * 2
                            raw_bytes = (ctypes.c_uint8 * data_size).from_address(buffer_ptr)
                            audio_arr = np.frombuffer(bytes(raw_bytes), dtype=np.int16).copy()
                            try:
                                audio_arr = audio_arr.reshape(-1, audio_channels)
                                audio_callback(audio_arr, 48000, audio_channels,
                                               audio_pts, TIMESCALE, audio_pts_valid)
                            except ValueError as e:
                                logger.warning("Audio reshape error: %s", e)

                    except Exception as e:
                        logger.error("Error processing audio packet: %s", e, exc_info=True)

                elif audio_callback:
                    # audioPacket absent on this video frame arrival — audio delivery skipped.
                    _missing_audio_pkts[0] += 1
                    _log_graduated(
                        "ffrecord.capture", _missing_audio_pkts[0],
                        "audioPacket absent in VideoInputFrameArrived",
                        Exception(f"frame_n={_frame_n[0]} total_absent={_missing_audio_pkts[0]}"),
                    )

            except Exception as e:
                logger.error("Error in VideoInputFrameArrived: %s", e, exc_info=True)
            return 0

    return _CB()


def from_comtypes_or_default(name: str, default: int) -> int:
    if _dl:
        return getattr(_dl, name, default)
    return default


def _log_graduated(logger_name: str, count: int, msg: str, exc: Exception) -> None:
    log = logging.getLogger(logger_name)
    if count in (1, 10, 100, 1000) or (count > 1000 and count % 1000 == 0):
        level = logging.ERROR if count >= 1000 else logging.WARNING
        log.log(level, "[av_sync_warn] %s (#%d): %s", msg, count, exc)


class DeckLinkCapture:
    """COM-only DeckLink capture for ffrecord.

    Wraps the device lifecycle; delivers frames and audio packets via callbacks
    to the fan-out layer in service.py.
    """

    def __init__(self, device_index: int = 0, audio_channels: int = 8,
                 format_change_callback: Optional[Callable] = None,
                 fallback_mode: str = "bmdModeHD1080i50"):
        if not HAS_COMTYPES or not _dl:
            raise RuntimeError("comtypes and DeckLink type library are required.")

        self.device_index = device_index
        self.audio_channels = max(1, min(audio_channels, 16))
        self.format_change_callback = format_change_callback
        self._fallback_mode_name = fallback_mode

        self._decklink_input = None
        self._display_name = "Unknown"
        self._callback_com = None
        self._notification_com = None   # IDeckLinkNotificationCallback COM object
        self._notification_if = None    # IDeckLinkNotification interface
        self._started = False
        # Guards start()/stop() so concurrent callers (HTTP /stop racing with SIGINT,
        # signal handler racing with shutdown thread) cannot double-initialise or
        # double-tear-down the COM device. Held across the entire start/stop body so
        # stop() blocks until any in-progress start() completes.
        self._lifecycle_lock = threading.Lock()
        # Single-element lists shared with the COM callback closure so the Service
        # heartbeat (L1) and /status endpoint (B8) can read these without traversing
        # the closure or holding a lock.
        self._shared_audio_overflow_dropped: list = [0]
        self._shared_last_callback_monotonic: list = [0.0]
        self._shared_framerate: list = [None]   # shared with the callback closure
        self._shared_field_dominance: list = [None]  # BMDFieldDominance from current display mode

        self._frame_callback: Optional[Callable] = None
        self._audio_callback: Optional[Callable] = None

        self._init()

    def _init(self) -> None:
        from .decklink_comtypes import get_device_by_index
        self._decklink_input, self._display_name = get_device_by_index(self.device_index)
        logger.info("DeckLink device ready: %s", self._display_name)

    @property
    def display_name(self) -> str:
        return self._display_name

    @property
    def field_dominance(self) -> Optional[int]:
        """BMDFieldDominance FourCC from the current display mode, or None if unknown.
        0x75707072 'uppr' = bmdUpperFieldFirst (TFF),
        0x6C6F7772 'lowr' = bmdLowerFieldFirst (BFF),
        0x70726F67 'prog' = bmdProgressiveFrame,
        0x70736620 'psf ' = bmdProgressiveSegmentedFrame.
        """
        return self._shared_field_dominance[0]

    def start(self, frame_callback: Callable, audio_callback: Callable) -> None:
        with self._lifecycle_lock:
            if self._started:
                logger.warning("Already started")
                return

            self._frame_callback = frame_callback
            self._audio_callback = audio_callback

            # bmdModeUnknown tells the SDK there is no assumed format, so VideoInputFormatChanged
            # is guaranteed to fire for any live signal — even when the signal matches the old hint.
            # Fall back to self._fallback_mode_name (from channel.expected_format) for devices that reject bmdModeUnknown.
            _bmd_mode_unknown = getattr(_dl, 'bmdModeUnknown', 0x69756E6B)
            try:
                hr = self._decklink_input.EnableVideoInput(
                    _bmd_mode_unknown,
                    _dl.bmdFormat8BitYUV,
                    _dl.bmdVideoInputEnableFormatDetection,
                )
                if hr != 0:
                    raise RuntimeError(f"hr=0x{hr:08x}")
                logger.debug("EnableVideoInput: bmdModeUnknown accepted")
            except Exception as e:
                fallback_mode_val = getattr(_dl, self._fallback_mode_name, None)
                if fallback_mode_val is None:
                    raise RuntimeError(
                        f"Unknown fallback_mode '{self._fallback_mode_name}' — "
                        "check channel.expected_format in your config"
                    )
                logger.debug(
                    "bmdModeUnknown rejected (%s) — falling back to %s",
                    e, self._fallback_mode_name,
                )
                try:
                    self._decklink_input.DisableVideoInput()
                except Exception:
                    pass
                _deadline = time.monotonic() + 10.0
                _retry_interval = 0.5
                while True:
                    try:
                        hr = self._decklink_input.EnableVideoInput(
                            fallback_mode_val,
                            _dl.bmdFormat8BitYUV,
                            _dl.bmdVideoInputEnableFormatDetection,
                        )
                        if hr != 0:
                            raise RuntimeError(f"EnableVideoInput returned: {hr:#010x}")
                        break
                    except Exception as retry_e:
                        if time.monotonic() >= _deadline:
                            raise RuntimeError(
                                "EnableVideoInput failed after 10s retries "
                                f"(device may be held by another process): {retry_e}"
                            ) from retry_e
                        logger.warning(
                            "EnableVideoInput failed (%s) — device may still be releasing, retrying in %.1fs...",
                            retry_e, _retry_interval,
                        )
                        time.sleep(_retry_interval)

            hr = self._decklink_input.EnableAudioInput(
                _dl.bmdAudioSampleRate48kHz,
                _dl.bmdAudioSampleType16bitInteger,
                self.audio_channels,
            )
            if hr != 0:
                logger.warning("EnableAudioInput failed: %s", hr)
            else:
                logger.info("Audio input enabled (%d channels)", self.audio_channels)

            self._shared_framerate[0] = None  # reset in case of restart
            self._shared_field_dominance[0] = None
            self._callback_com = _create_input_callback(
                frame_callback, audio_callback, self.audio_channels,
                self._decklink_input, self.format_change_callback,
                shared_framerate=self._shared_framerate,
                shared_field_dominance=self._shared_field_dominance,
                shared_audio_overflow_dropped=self._shared_audio_overflow_dropped,
                shared_last_callback_monotonic=self._shared_last_callback_monotonic,
            )
            hr = self._decklink_input.SetCallback(self._callback_com)
            if hr != 0:
                logger.warning("SetCallback failed: %s", hr)

            hr = self._decklink_input.StartStreams()
            if hr != 0:
                raise RuntimeError(f"StartStreams failed: {hr:#010x}")

            self._started = True
            logger.info("DeckLink capture started")

        self._setup_notification()

        threading.Thread(
            target=self._probe_format_if_needed,
            name="format-probe", daemon=True,
        ).start()

    def _setup_notification(self) -> None:
        """Subscribe to IDeckLinkNotification(bmdStatusChanged) as a supplementary signal-lock monitor.

        VideoInputFormatChanged is the primary signal-change path, but some driver versions or signal
        conditions fire bmdStatusChanged without triggering VideoInputFormatChanged. This provides a
        second pathway to log signal lock/unlock events.
        """
        try:
            from comtypes import COMObject

            bmd_status_changed = getattr(_dl, 'bmdStatusChanged', 0x73746174)
            bmd_signal_locked_id = getattr(_dl, 'bmdDeckLinkStatusVideoInputSignalLocked', 0x7669736C)
            capture_ref = self

            class _NotifyCB(COMObject):
                _com_interfaces_ = [_dl.IDeckLinkNotificationCallback]

                def Notify(self, topic, param1, param2):
                    try:
                        if topic == bmd_status_changed and int(param1) == bmd_signal_locked_id:
                            locked = capture_ref.get_signal_locked()
                            if locked is False:
                                log_signal_loss("signal_not_locked (IDeckLinkNotification)")
                            elif locked:
                                logger.info("[sync] STATUS_CHANGE signal_locked=True")
                    except Exception as e:
                        logger.debug("Notification callback error: %s", e)
                    return 0

            cb = _NotifyCB()
            notification = self._decklink_input.QueryInterface(_dl.IDeckLinkNotification)
            notification.Subscribe(bmd_status_changed, cb)
            self._notification_com = cb
            self._notification_if = notification
            logger.info("IDeckLinkNotification subscribed for bmdStatusChanged")
        except Exception as e:
            logger.debug("IDeckLinkNotification not available on this card/driver: %s", e)

    def _probe_format_if_needed(self) -> None:
        """Probe the current input format via IDeckLinkStatus if VideoInputFormatChanged doesn't fire within 1s.

        Some DeckLink driver versions skip the initial VideoInputFormatChanged callback when the
        incoming signal format matches the mode passed to EnableVideoInput. This probe covers that gap.
        """
        import time
        time.sleep(1.0)

        if self._shared_framerate[0] is not None or not self._started:
            return

        logger.info("VideoInputFormatChanged not received within 1s — probing input format via IDeckLinkStatus")

        # Check signal lock before probing format; log if no signal present at all
        locked = self.get_signal_locked()
        if locked is False:
            log_signal_loss("signal_not_locked (startup probe — no SDI signal detected)")
        elif locked is None:
            logger.debug("bmdDeckLinkStatusVideoInputSignalLocked not available on this card")

        # Step 1: query the currently detected video input mode constant
        try:
            status = self._decklink_input.QueryInterface(_dl.IDeckLinkStatus)
            mode_constant = status.GetInt(
                getattr(_dl, 'bmdDeckLinkStatusCurrentVideoInputMode', 0x76697666)
            )
        except Exception as e:
            logger.warning("IDeckLinkStatus probe failed: %s", e)
            return

        if not mode_constant:
            logger.warning("IDeckLinkStatus reports no signal (mode=0) — check SDI cable and source")
            return

        # Step 2: walk IDeckLinkInput's display mode iterator to find the matching mode and read its framerate
        try:
            dl_input = self._decklink_input.QueryInterface(_dl.IDeckLinkInput)
            mode_iter = dl_input.GetDisplayModeIterator()
        except Exception as e:
            logger.warning("GetDisplayModeIterator failed: %s", e)
            return

        try:
            while True:
                try:
                    dm = mode_iter.Next()
                except Exception:
                    break
                if dm is None:
                    break
                try:
                    if dm.GetDisplayMode() != mode_constant:
                        continue
                    mode_name = dm.GetName()
                    raw = dm.GetFrameRate()
                    fps_num, fps_den = int(raw[1]), int(raw[0])
                    self._shared_framerate[0] = (fps_num, fps_den)
                    try:
                        self._shared_field_dominance[0] = int(dm.GetFieldDominance())
                    except Exception:
                        pass
                    logger.info("Probed input format: '%s' fps=%d/%d — frames will now flow", mode_name, fps_num, fps_den)
                    if self.format_change_callback:
                        try:
                            self.format_change_callback(f"{mode_name} (probed at startup)")
                        except Exception as cb_err:
                            logger.error("format_change_callback error: %s", cb_err)
                    return
                except Exception as e:
                    logger.debug("Mode entry query error: %s", e)
                    continue
            logger.warning("Signal detected (mode=0x%08x) but not found in display mode iterator", mode_constant)
        except Exception as e:
            logger.warning("Display mode enumeration failed: %s", e)

    def stop(self) -> None:
        with self._lifecycle_lock:
            if not self._started:
                return
            if self._notification_if and self._notification_com:
                try:
                    self._notification_if.Unsubscribe(
                        getattr(_dl, 'bmdStatusChanged', 0x73746174),
                        self._notification_com,
                    )
                except Exception as e:
                    logger.warning("Notification unsubscribe failed: %s", e)
                self._notification_if = None
                self._notification_com = None
            try:
                self._decklink_input.StopStreams()
                self._decklink_input.DisableVideoInput()
                self._decklink_input.DisableAudioInput()
            except Exception as e:
                logger.error("Error stopping DeckLink: %s", e)
            self._started = False
            logger.info("DeckLink capture stopped")

    def get_signal_locked(self) -> Optional[bool]:
        """Query IDeckLinkStatus for signal lock state.

        Returns True if locked, False if not locked, None if the query failed
        (e.g. IDeckLinkStatus not available on this card/driver version).
        """
        try:
            status = self._decklink_input.QueryInterface(_dl.IDeckLinkStatus)
            locked = status.GetFlag(
                getattr(_dl, 'bmdDeckLinkStatusVideoInputSignalLocked', 0x7669736C)
            )
            return bool(locked)
        except Exception:
            return None

    def get_status(self) -> dict:
        last_cb = self._shared_last_callback_monotonic[0]
        callback_age_s = (time.monotonic() - last_cb) if last_cb > 0 else None
        return {
            "device": self._display_name,
            "device_index": self.device_index,
            "started": self._started,
            "audio_channels": self.audio_channels,
            "audio_overflow_dropped_callbacks": self._shared_audio_overflow_dropped[0],
            "last_callback_age_s": callback_age_s,
        }

    @property
    def last_callback_monotonic(self) -> float:
        """Monotonic timestamp of the most recent DeckLink callback.

        Used by the Service-level heartbeat / frame-arrival watchdog (L1/L2).
        Returns 0.0 before the first callback fires.
        """
        return self._shared_last_callback_monotonic[0]

    @property
    def audio_overflow_dropped(self) -> int:
        """Total callbacks skipped by the startup audio-overflow latch."""
        return self._shared_audio_overflow_dropped[0]
