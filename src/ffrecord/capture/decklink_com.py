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
    log_audio_packet, log_av_lag, log_missed_frames, log_psf_frame,
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
):
    """Return a COM-backed IDeckLinkInputCallback."""
    from comtypes import COMObject

    _framerate = shared_framerate if shared_framerate is not None else [None]
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
                if _framerate[0] is None:
                    return 0

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

                        # Frame buffer
                        video_buffer = videoFrame.QueryInterface(_dl.IDeckLinkVideoBuffer)
                        video_buffer.StartAccess(_dl.bmdBufferAccessRead)
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

                        video_buffer.EndAccess(_dl.bmdBufferAccessRead)

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
                        if _diag_audio[0] < _DIAG_BURST:
                            _diag_audio[0] += 1
                            log_audio_packet(
                                _audio_n[0], audio_pts, hw_ref_audio, hw_ref_audio_valid,
                                TIMESCALE, sample_count,
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
        self._shared_framerate: list = [None]   # shared with the callback closure

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

    def start(self, frame_callback: Callable, audio_callback: Callable) -> None:
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
        self._callback_com = _create_input_callback(
            frame_callback, audio_callback, self.audio_channels,
            self._decklink_input, self.format_change_callback,
            shared_framerate=self._shared_framerate,
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
        if not self._started:
            return
        if self._notification_if and self._notification_com:
            try:
                self._notification_if.Unsubscribe(
                    getattr(_dl, 'bmdStatusChanged', 0x73746174),
                    self._notification_com,
                )
            except Exception:
                pass
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
        return {
            "device": self._display_name,
            "device_index": self.device_index,
            "started": self._started,
            "audio_channels": self.audio_channels,
        }
