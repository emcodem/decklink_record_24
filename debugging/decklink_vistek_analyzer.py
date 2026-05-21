"""Live DeckLink Vistek A/V sync analyzer.

Receives frames directly from DeckLinkCapture (raw UYVY video + int16 audio)
and detects the Vistek A/V sync pulse using the same logic as vistek_analyzer.py.

Key differences from vistek_analyzer.py:
  - No PyAV: video comes as raw bmdFormat8BitYUV (UYVY) bytes; luma is extracted
    directly instead of decoding to RGB24.
  - No file seek: runs in real-time until Ctrl+C or stop() is called.
  - Timestamps come from DeckLink stream-time ticks (10 MHz clock) rather than
    PTS embedded in a container.
  - All analysis runs on hw_pts-paired AVPair objects produced by CaptureBuffer,
    ensuring each video frame is matched with exactly its temporal audio before
    the detectors see either stream.

Usage (standalone):
    python decklink_vistek_analyzer.py [--device-index 0] [--audio-channels 8]

Importable API:
    from decklink_vistek_analyzer import DecklinkVistekAnalyzer, AnalyzerConfig
"""

from __future__ import annotations

import argparse
import logging
import math
import pathlib
import signal
import sys
import threading
from typing import Callable, List, Optional

import numpy as np

# Allow imports from the same debugging/ directory and from src/
_HERE = pathlib.Path(__file__).parent
_SRC = _HERE.parent / "src"
for _p in (str(_SRC), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from vistek_analyzer import AnalyzerConfig, Correlator, DelayEvent
from ffrecord.output.base import AudioPacket as _AudioPacket, AVPair as _AVPair, VideoFrame as _VideoFrame

__all__ = [
    "DecklinkVistekAnalyzer",
    "LiveBlackCrossDetector",
    "LiveAudioSilenceDetector",
    "AnalyzerConfig",
    "DelayEvent",
]

TIMESCALE = 10_000_000  # DeckLink 10 MHz hardware clock

logger = logging.getLogger("decklink_vistek")


# ─────────────────────────────────────────────────────────────────────────────
# Video: detect the middle scan-line going black (UYVY input)
# ─────────────────────────────────────────────────────────────────────────────


class LiveBlackCrossDetector:
    """Fires when two consecutive UYVY frames have the centre pixel below the
    white threshold, using the second frame's timestamp as the event time.

    DeckLink delivers bmdFormat8BitYUV (UYVY): [U0, Y0, V0, Y1] per 2 pixels.
    Only the Y (luma) component is sampled at the configured column.

    AnalyzerConfig.colorbar_white_min is an RGB [0-255] value. It is mapped to
    broadcast-range Y-luma [16-235] so that a 242/255 RGB threshold translates
    to ≈224 Y.
    """

    def __init__(self, cfg: AnalyzerConfig):
        self.cfg = cfg
        self._consec_cross = 0
        self.last_cross_pts_s: Optional[float] = None
        # Map RGB [0-255] colorbar threshold to broadcast Y [16-235]
        self._y_white_min = int(cfg.colorbar_white_min / 255.0 * 219 + 16)

    def feed(
        self,
        frame_bytes: bytes,
        width: int,
        height: int,
        row_bytes: int,
        stream_time_s: float,
    ) -> Optional[float]:
        """Process one raw UYVY frame. Returns cross timestamp (s) or None."""
        y_row = height // 2
        x_col = min(self.cfg.sample_x, width - 1)

        # UYVY: [U, Y0, V, Y1] per 2-pixel macro-block
        macro = x_col // 2
        y_byte_in_row = macro * 4 + (1 if x_col % 2 == 0 else 3)
        luma = frame_bytes[y_row * row_bytes + y_byte_in_row]

        is_cross = luma < self._y_white_min

        if is_cross:
            self._consec_cross += 1
        else:
            self._consec_cross = 0

        if self._consec_cross == 2:
            self.last_cross_pts_s = stream_time_s
            return stream_time_s
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Audio: detect channel-1 silence (int16 numpy array input)
# ─────────────────────────────────────────────────────────────────────────────


class LiveAudioSilenceDetector:
    """Fires on the rising edge of CH1 RMS dropping below threshold.

    Equivalent to AudioSilenceDetector but consumes int16 numpy arrays
    (shape n_samples × n_channels) delivered by DeckLinkCapture instead of
    PyAV AudioFrame objects.
    """

    def __init__(self, cfg: AnalyzerConfig, sample_rate: int = 48000):
        self.cfg = cfg
        self.sr = sample_rate
        self.window_samples = max(1, int(round(sample_rate * cfg.rms_window_ms / 1000.0)))
        self.threshold_lin = 10.0 ** (cfg.audio_threshold_db / 20.0)
        self._buf = np.empty(0, dtype=np.float32)
        self._buf_start_pts_s: Optional[float] = None
        self._silence_started = False
        self.last_silence_pts_s: Optional[float] = None

    def feed(
        self,
        audio_arr: np.ndarray,
        packet_pts_s: float,
        n_channels: int,
    ) -> List[float]:
        """Process one int16 audio packet (n_samples × n_channels).

        Returns a list of silence-onset timestamps (seconds).
        """
        events: List[float] = []
        ch_idx = min(self.cfg.audio_channel, n_channels - 1)

        # audio_arr: (n_samples, n_channels) int16 → float32 [-1, 1]
        raw_ch = audio_arr[:, ch_idx].astype(np.float32) / 32768.0

        if self._buf.size == 0:
            self._buf_start_pts_s = packet_pts_s
        self._buf = np.concatenate([self._buf, raw_ch])

        win = self.window_samples
        while self._buf.size >= win:
            chunk = self._buf[:win]
            rms = math.sqrt(float(np.mean(chunk * chunk)))
            is_silent = rms < self.threshold_lin

            if is_silent and not self._silence_started:
                self._silence_started = True
                self.last_silence_pts_s = self._buf_start_pts_s
                events.append(self._buf_start_pts_s)
            elif not is_silent and self._silence_started:
                self._silence_started = False

            self._buf = self._buf[win:]
            self._buf_start_pts_s += win / self.sr

        return events


# ─────────────────────────────────────────────────────────────────────────────
# Top-level analyzer
# ─────────────────────────────────────────────────────────────────────────────


class DecklinkVistekAnalyzer:
    """Wires DeckLinkCapture to LiveBlackCrossDetector and LiveAudioSilenceDetector.

    Call start() to begin capture, stop() to end. Delay events are printed to
    stdout and optionally forwarded to event_callback.
    """

    def __init__(
        self,
        device_index: int = 0,
        audio_channels: int = 8,
        config: Optional[AnalyzerConfig] = None,
        csv_mode: bool = False,
        stats: Optional[dict] = None,
        event_callback: Optional[Callable[[DelayEvent], None]] = None,
    ):
        self.cfg = config or AnalyzerConfig()
        self.csv_mode = csv_mode
        self.stats = stats if stats is not None else {
            "n_crosses": 0, "n_silences": 0, "crosses": [], "silences": [],
        }
        self.event_callback = event_callback

        self._audio_channels = audio_channels
        self.debug = False  # set True to print raw luma + audio RMS every frame
        self._video_det: Optional[LiveBlackCrossDetector] = None
        self._audio_det: Optional[LiveAudioSilenceDetector] = None
        self._correlator = Correlator(period_ms=self.cfg.vistek_period_ms)
        self._lock = threading.Lock()

        from ffrecord.capture_buffer import CaptureBuffer
        self._capture_buf = CaptureBuffer(
            emit_callback=self._on_av_pair,
            default_audio_channels=audio_channels,
            default_audio_sample_rate=48000,
            hw_pts_rate=TIMESCALE,
        )

        from ffrecord.capture.decklink_com import DeckLinkCapture
        self._capture = DeckLinkCapture(
            device_index=device_index,
            audio_channels=audio_channels,
        )

    def start(self) -> None:
        self._video_det = LiveBlackCrossDetector(self.cfg)
        self._audio_det = LiveAudioSilenceDetector(self.cfg, sample_rate=48000)
        self._capture.start(self._on_video_frame, self._on_audio_packet)

    def stop(self) -> None:
        self._capture.stop()

    def _on_video_frame(
        self,
        frame_bytes: bytes,
        width: int,
        height: int,
        pixel_format: int,
        framerate,
        flags: int,
        row_bytes: int,
        video_stream_time: int,
        timescale: int,
        stream_time_valid: bool,
        hw_ref_time: int,
        hw_ref_valid: bool,
        tc_str: str,
    ) -> None:
        if not stream_time_valid:
            return
        data = np.frombuffer(frame_bytes, dtype=np.uint8).reshape(height, row_bytes).copy()
        vf = _VideoFrame(
            data=data, fmt="uyvy422", width=width, height=height,
            framerate=framerate,
            hw_pts=video_stream_time, hw_pts_rate=timescale,
            hw_pts_valid=stream_time_valid, timecode=tc_str,
        )
        self._capture_buf.push_video(vf)

    def _on_audio_packet(
        self,
        audio_arr: np.ndarray,
        sample_rate: int,
        n_channels: int,
        audio_pts: int,
        timescale: int,
        audio_pts_valid: bool,
    ) -> None:
        if not audio_pts_valid:
            return
        pkt = _AudioPacket(
            data=audio_arr, sample_rate=sample_rate, channels=n_channels,
            hw_pts=audio_pts, hw_pts_rate=timescale, hw_pts_valid=audio_pts_valid,
        )
        self._capture_buf.push_audio(pkt)

    def _on_av_pair(self, pair: "_AVPair") -> None:
        """Emit callback from CaptureBuffer — feeds paired AVPair into detectors.

        Called while CaptureBuffer's internal lock is held, so we must not
        call push_video/push_audio from here (no re-entrant lock needed because
        we only acquire self._lock, which is never held before push_video/push_audio
        in the buffer code path).
        """
        if self._video_det is None or self._audio_det is None:
            return
        V = pair.video
        A = pair.audio
        pts_s = V.hw_pts / V.hw_pts_rate
        row_bytes = V.data.shape[1]
        frame_bytes = V.data.tobytes()

        with self._lock:
            if self.debug:
                det = self._video_det
                x = min(self.cfg.sample_x, V.width - 1)
                macro = x // 2
                y_byte = macro * 4 + (1 if x % 2 == 0 else 3)
                luma = frame_bytes[(V.height // 2) * row_bytes + y_byte]
                print(
                    f"[dbg video] pts={pts_s:.3f}s  luma={luma}"
                    f"  y_white_min={det._y_white_min}  consec={det._consec_cross}"
                    f"  synthesized={pair.audio_is_synthesized}",
                    flush=True,
                )

            cross_pts = self._video_det.feed(frame_bytes, V.width, V.height, row_bytes, pts_s)
            if cross_pts is not None:
                self.stats["n_crosses"] += 1
                self.stats["crosses"].append(cross_pts)
                silence_pts = self._audio_det.last_silence_pts_s
                ev = self._correlator.update(silence_pts, cross_pts, "cross")
                if ev:
                    self._emit(ev)

            if not pair.audio_is_synthesized:
                audio_pts_s = A.hw_pts / A.hw_pts_rate
                if self.debug:
                    ch_idx = min(self.cfg.audio_channel, A.channels - 1)
                    raw = A.data[:, ch_idx].astype(np.float32) / 32768.0
                    rms_db = 20 * math.log10(max(math.sqrt(float(np.mean(raw * raw))), 1e-10))
                    print(
                        f"[dbg audio] pts={audio_pts_s:.3f}s  ch={ch_idx}"
                        f"  rms={rms_db:.1f}dBFS  threshold={self.cfg.audio_threshold_db}dB"
                        f"  silence_started={self._audio_det._silence_started}",
                        flush=True,
                    )
                for s_pts in self._audio_det.feed(A.data, audio_pts_s, A.channels):
                    self.stats["n_silences"] += 1
                    self.stats["silences"].append(s_pts)
                    cross_pts2 = self._video_det.last_cross_pts_s
                    ev = self._correlator.update(s_pts, cross_pts2, "silence")
                    if ev:
                        self._emit(ev)

    def _emit(self, ev: DelayEvent) -> None:
        if self.event_callback:
            self.event_callback(ev)
        if self.csv_mode:
            print(
                f"{ev.pts_s:.3f},{ev.delay_ms:.1f},"
                f"{ev.silence_pts_s:.3f},{ev.cross_pts_s:.3f},{ev.trigger}",
                flush=True,
            )
        else:
            sign = "+" if ev.delay_ms >= 0 else ""
            print(
                f"{_format_timestamp(ev.pts_s)}  "
                f"A/V delay: {sign}{ev.delay_ms:7.1f} ms  "
                f"(silence@{ev.silence_pts_s:.3f}s  cross@{ev.cross_pts_s:.3f}s  "
                f"via {ev.trigger})",
                flush=True,
            )


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _format_timestamp(pts_s: float) -> str:
    if pts_s < 0:
        return "-" + _format_timestamp(-pts_s)
    h = int(pts_s // 3600)
    m = int((pts_s % 3600) // 60)
    s = pts_s - h * 3600 - m * 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Live DeckLink Vistek A/V sync analyzer.",
    )
    p.add_argument("--device-index", type=int, default=0,
                   help="DeckLink device index (default: 0).")
    p.add_argument("--audio-channels", type=int, default=8,
                   help="Number of audio channels to open (default: 8).")
    p.add_argument("--csv", action="store_true",
                   help="Emit machine-readable CSV instead of human-readable lines.")
    p.add_argument("--audio-threshold-db", type=float, default=-50.0,
                   help="RMS dBFS below which audio is considered silent (default: -50).")
    p.add_argument("--pixel-black-max", type=int, default=39,
                   help="Y-luma max to count a pixel as black (default: 39).")
    p.add_argument("--colorbar-white-min", type=int, default=242,
                   help="RGB-scale white minimum used to derive Y-luma threshold (default: 242).")
    p.add_argument("--sample-x", type=int, default=10,
                   help="Column to sample for the black-cross test (default: 10).")
    p.add_argument("--audio-channel", type=int, default=0,
                   help="Audio channel index to analyze (default: 0 = CH1).")
    p.add_argument("--rms-window-ms", type=float, default=2.0,
                   help="RMS analysis window length in ms (default: 2.0).")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress the summary line on exit.")
    p.add_argument("--debug", action="store_true",
                   help="Print raw luma and audio RMS for every pair (very verbose).")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                   help="Log level for capture diagnostics (default: INFO).")
    p.add_argument("--duration", type=float, default=None,
                   help="Stop automatically after this many seconds.")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    cfg = AnalyzerConfig(
        audio_threshold_db=args.audio_threshold_db,
        pixel_black_max=args.pixel_black_max,
        colorbar_white_min=args.colorbar_white_min,
        sample_x=args.sample_x,
        audio_channel=args.audio_channel,
        rms_window_ms=args.rms_window_ms,
    )

    stats: dict = {"n_crosses": 0, "n_silences": 0, "crosses": [], "silences": []}
    analyzer = DecklinkVistekAnalyzer(
        device_index=args.device_index,
        audio_channels=args.audio_channels,
        config=cfg,
        csv_mode=args.csv,
        stats=stats,
    )
    analyzer.debug = args.debug

    if args.csv:
        print("event_pts_s,av_delay_ms,silence_pts_s,cross_pts_s,trigger")

    stop_event = threading.Event()

    def _on_signal(sig, frame):
        print("\n[decklink_vistek] stopping...", file=sys.stderr)
        stop_event.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    analyzer.start()
    dur_str = f"{args.duration:.0f}s" if args.duration else "Ctrl+C"
    print(f"[decklink_vistek] capturing — stop with {dur_str}", file=sys.stderr)

    if args.duration:
        threading.Timer(args.duration, lambda: stop_event.set()).start()

    try:
        stop_event.wait()
    finally:
        analyzer.stop()

    if not args.quiet:
        crosses = stats["crosses"]
        silences = stats["silences"]
        print(
            f"[decklink_vistek] video crosses  ({len(crosses)}): "
            + ", ".join(f"{t:.3f}s" for t in crosses[:20]),
            file=sys.stderr,
        )
        print(
            f"[decklink_vistek] audio silences ({len(silences)}): "
            + ", ".join(f"{t:.3f}s" for t in silences[:20]),
            file=sys.stderr,
        )

    return 0 if stats["n_crosses"] > 0 or stats["n_silences"] > 0 else 2


if __name__ == "__main__":
    sys.exit(main())
