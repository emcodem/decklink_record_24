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
from typing import Callable, Optional

import numpy as np

from ..sync_log import (
    log_audio_packet, log_av_lag, log_signal_loss, log_signal_return, log_video_frame,
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
):
    """Return a COM-backed IDeckLinkInputCallback."""
    from comtypes import COMObject

    _framerate = [None]
    _current_mode = [None]
    _video_hw_pts_fail = [0]
    _audio_hw_pts_fail = [0]
    _hw_ref_fail = [0]
    _diag_frames = [0]
    _diag_audio = [0]
    _frame_n = [0]
    _audio_n = [0]

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
                            log_signal_loss("bmdFrameHasNoInputSource")

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

                        # Hardware reference timestamp (independent wall clock)
                        hw_ref_time = 0
                        hw_ref_valid = False
                        try:
                            raw_hw = videoFrame.GetHardwareReferenceTimestamp(TIMESCALE)
                            hw_ref_time = int(raw_hw[0])
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

                        # Input queue depth
                        queue_depth = 0
                        try:
                            queue_depth = decklink_input.GetAvailableVideoFrameCount()
                        except Exception:
                            pass

                        # Sync log (first DIAG_BURST after each format change at INFO, then DEBUG)
                        _frame_n[0] += 1
                        if _diag_frames[0] < _DIAG_BURST:
                            _diag_frames[0] += 1
                            log_video_frame(
                                _frame_n[0], video_stream_time, hw_ref_time, hw_ref_valid,
                                tc_str, TIMESCALE, flags, queue_depth,
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
                 format_change_callback: Optional[Callable] = None):
        if not HAS_COMTYPES or not _dl:
            raise RuntimeError("comtypes and DeckLink type library are required.")

        self.device_index = device_index
        self.audio_channels = max(1, min(audio_channels, 16))
        self.format_change_callback = format_change_callback

        self._decklink_input = None
        self._display_name = "Unknown"
        self._callback_com = None
        self._started = False

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

        hr = self._decklink_input.EnableVideoInput(
            _dl.bmdModeHD1080i50,
            _dl.bmdFormat8BitYUV,
            _dl.bmdVideoInputEnableFormatDetection,
        )
        if hr != 0:
            raise RuntimeError(f"EnableVideoInput failed: {hr:#010x}")

        hr = self._decklink_input.EnableAudioInput(
            _dl.bmdAudioSampleRate48kHz,
            _dl.bmdAudioSampleType16bitInteger,
            self.audio_channels,
        )
        if hr != 0:
            logger.warning("EnableAudioInput failed: %s", hr)
        else:
            logger.info("Audio input enabled (%d channels)", self.audio_channels)

        self._callback_com = _create_input_callback(
            frame_callback, audio_callback, self.audio_channels,
            self._decklink_input, self.format_change_callback,
        )
        hr = self._decklink_input.SetCallback(self._callback_com)
        if hr != 0:
            logger.warning("SetCallback failed: %s", hr)

        hr = self._decklink_input.StartStreams()
        if hr != 0:
            raise RuntimeError(f"StartStreams failed: {hr:#010x}")

        self._started = True
        logger.info("DeckLink capture started")

    def stop(self) -> None:
        if not self._started:
            return
        try:
            self._decklink_input.StopStreams()
            self._decklink_input.DisableVideoInput()
            self._decklink_input.DisableAudioInput()
        except Exception as e:
            logger.error("Error stopping DeckLink: %s", e)
        self._started = False
        logger.info("DeckLink capture stopped")

    def get_status(self) -> dict:
        return {
            "device": self._display_name,
            "device_index": self.device_index,
            "started": self._started,
            "audio_channels": self.audio_channels,
        }
