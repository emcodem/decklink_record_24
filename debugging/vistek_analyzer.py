"""Standalone Vistek A/V sync analyzer.

Vistek is a broadcast test pattern that emits synchronized cross-modal pulses
every ~4 seconds: the middle scan-line of the video turns black at the same
instant as audio channel 1 mutes. Measuring the time offset between the two
events reveals the A/V delay of the capture / playback chain with
millisecond precision.

This file is a self-contained module — drop it into any project that has
``av`` (PyAV) and ``numpy`` installed and call ``analyze_file()`` or run
``python vistek_analyzer.py <file>`` from the shell.

Reference implementation: ``Screener/MPlatform_Helpers/VistekAnalyzer.cs``.
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from dataclasses import dataclass
from fractions import Fraction
from typing import Iterator, List, Optional

import av
import numpy as np


__all__ = [
    "analyze_file",
    "AnalyzerConfig",
    "DelayEvent",
    "BlackCrossDetector",
    "AudioSilenceDetector",
    "Correlator",
]


# ─────────────────────────────────────────────────────────────────────────────
# Public types
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class AnalyzerConfig:
    """Tunable detection thresholds. Defaults match the C# reference."""

    audio_threshold_db: float = -50.0
    pixel_black_max: int = 39
    sample_x: int = 10
    rms_window_ms: float = 2.0
    audio_channel: int = 0
    vistek_period_ms: float = 4000.0
    colorbar_white_min: int = 242


@dataclass
class DelayEvent:
    """A single A/V delay measurement, emitted when the delay changes."""

    pts_s: float
    delay_ms: float
    silence_pts_s: float
    cross_pts_s: float
    trigger: str  # 'cross' or 'silence'


# ─────────────────────────────────────────────────────────────────────────────
# Video: detect the middle scan-line going black
# ─────────────────────────────────────────────────────────────────────────────


class BlackCrossDetector:
    """Fires when two consecutive frames have the centre pixel below the white
    threshold, using the second frame's PTS as the event time.

    Requiring two consecutive non-white frames suppresses single-frame
    encoder pre-ringing artefacts that would otherwise cause one-frame-early
    detection (40 ms at 25 fps).
    """

    def __init__(self, cfg: AnalyzerConfig):
        self.cfg = cfg
        self._consec_cross: int = 0  # consecutive non-white frames seen
        self.last_cross_pts_s: Optional[float] = None

    def feed(self, frame: "av.VideoFrame", stream_time_base: Fraction) -> Optional[float]:
        """Process one decoded video frame.

        Returns the confirmed-cross timestamp in seconds, or ``None``.
        """
        arr = frame.to_ndarray(format="rgb24")
        h = arr.shape[0]
        w = arr.shape[1]
        y = h // 2
        x = min(self.cfg.sample_x, w - 1)
        thr_w = self.cfg.colorbar_white_min
        r, g, b = arr[y, x]
        # Cross: centre pixel drops below white threshold (~109 vs ~252 in prod).
        is_cross = not (int(r) >= thr_w and int(g) >= thr_w and int(b) >= thr_w)

        if is_cross:
            self._consec_cross += 1
        else:
            self._consec_cross = 0

        event: Optional[float] = None
        if self._consec_cross == 2:
            event = _frame_pts_s(frame, stream_time_base)
            self.last_cross_pts_s = event
        return event


# ─────────────────────────────────────────────────────────────────────────────
# Audio: detect channel-1 silence
# ─────────────────────────────────────────────────────────────────────────────


class AudioSilenceDetector:
    """Fires on the rising edge of CH1 RMS dropping below threshold.

    A rolling 2 ms window slides across the chosen channel. RMS is computed
    over each window; when the value crosses below ``audio_threshold_db`` and
    the previous window was above it, the start-of-silence timestamp is
    reported with sub-frame precision.
    """

    def __init__(self, cfg: AnalyzerConfig, sample_rate: int):
        self.cfg = cfg
        self.sr = sample_rate
        self.window_samples = max(
            1, int(round(sample_rate * cfg.rms_window_ms / 1000.0))
        )
        self.threshold_lin = 10.0 ** (cfg.audio_threshold_db / 20.0)
        self._buf = np.empty(0, dtype=np.float32)
        self._buf_start_pts_s: Optional[float] = None
        self._silence_started = False
        self.last_silence_pts_s: Optional[float] = None

    def _to_fltp(self, frame: "av.AudioFrame") -> list:
        # AudioResampler crashes in PyAV 17 on to_ndarray() — convert manually.
        raw = frame.to_ndarray()
        fmt = frame.format.name
        n_ch = max(1, len(frame.layout.channels))
        ns = frame.samples

        scale = (
            2.0 ** 31 if "s32" in fmt
            else 2.0 ** 15 if "s16" in fmt
            else 2.0 ** 7 if "s8" in fmt
            else 1.0
        )
        is_planar = fmt.endswith("p") or (raw.ndim == 2 and raw.shape[0] == n_ch and raw.shape[1] == ns)
        if is_planar:
            farr = raw.astype(np.float32)
            if scale != 1.0:
                farr /= scale
        else:
            flat = raw[0] if raw.ndim == 2 else raw
            farr = flat.astype(np.float32)
            if scale != 1.0:
                farr /= scale
            farr = farr.reshape(ns, n_ch).T  # → (n_ch, n_samples)

        class _F:
            __slots__ = ("_arr", "pts", "time_base")
            def __init__(self, arr, pts, tb):
                self._arr = arr; self.pts = pts; self.time_base = tb
            def to_ndarray(self): return self._arr

        return [_F(farr, frame.pts, frame.time_base)]

    def feed(self, frame: "av.AudioFrame", stream_time_base: Fraction) -> List[float]:
        """Process one decoded audio frame.

        Returns a list of rising-edge timestamps (seconds) for any silence
        transitions that fell within this frame.
        """
        events: List[float] = []
        for rf in self._to_fltp(frame):
            arr = rf.to_ndarray()
            # fltp shape: (channels, samples). Single-channel content may
            # come back as 1-D.
            if arr.ndim == 1:
                ch = arr.astype(np.float32, copy=False)
            else:
                idx = min(self.cfg.audio_channel, arr.shape[0] - 1)
                ch = arr[idx].astype(np.float32, copy=False)

            if self._buf.size == 0:
                self._buf_start_pts_s = _frame_pts_s(rf, stream_time_base)
            self._buf = np.concatenate([self._buf, ch])

            win = self.window_samples
            while self._buf.size >= win:
                chunk = self._buf[:win]
                rms = math.sqrt(float(np.mean(chunk * chunk)))
                is_silent = rms < self.threshold_lin
                if is_silent and not self._silence_started:
                    self._silence_started = True
                    pts_s = self._buf_start_pts_s
                    self.last_silence_pts_s = pts_s
                    events.append(pts_s)
                elif not is_silent and self._silence_started:
                    self._silence_started = False
                self._buf = self._buf[win:]
                self._buf_start_pts_s += win / self.sr
        return events


# ─────────────────────────────────────────────────────────────────────────────
# Correlator: pair latest cross / silence events into a delay measurement
# ─────────────────────────────────────────────────────────────────────────────


class Correlator:
    """Emits a ``DelayEvent`` only when the (silence − cross) value changes."""

    def __init__(self, period_ms: float = 4000.0):
        self._half_period_ms = period_ms / 2.0

    def update(
        self,
        silence_pts_s: Optional[float],
        cross_pts_s: Optional[float],
        trigger: str,
    ) -> Optional[DelayEvent]:
        if silence_pts_s is None or cross_pts_s is None:
            return None
        raw_ms = (silence_pts_s - cross_pts_s) * 1000.0
        # Wrap into (-period/2, +period/2]: a raw ±4000 ms means adjacent-cycle
        # pairing, which is equivalent to 0 ms true delay.
        hp = self._half_period_ms
        delay_ms = round((raw_ms + hp) % (2 * hp) - hp, 1)
        event_pts = silence_pts_s if trigger == "silence" else cross_pts_s
        return DelayEvent(
            pts_s=event_pts,
            delay_ms=delay_ms,
            silence_pts_s=silence_pts_s,
            cross_pts_s=cross_pts_s,
            trigger=trigger,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Top-level entry point
# ─────────────────────────────────────────────────────────────────────────────


def analyze_file(
    path: str, config: Optional[AnalyzerConfig] = None, *, stats: Optional[dict] = None
) -> Iterator[DelayEvent]:
    """Analyze a media file and yield each change in measured A/V delay.

    ``delay_ms`` is signed: positive means audio silence arrives later than
    the corresponding video black-cross (i.e. audio lags video).

    If *stats* is a dict it is populated with ``n_crosses`` and ``n_silences``
    counts as the file is processed, useful for diagnosing zero-event results.
    """
    cfg = config or AnalyzerConfig()
    if stats is not None:
        stats.update(n_crosses=0, n_silences=0, crosses=[], silences=[])
    with av.open(path) as container:
        vstream = container.streams.video[0] if container.streams.video else None
        astream = container.streams.audio[0] if container.streams.audio else None
        if vstream is None and astream is None:
            return

        video_det = BlackCrossDetector(cfg) if vstream is not None else None
        audio_det: Optional[AudioSilenceDetector] = None
        correlator = Correlator(period_ms=cfg.vistek_period_ms)

        v_tb = vstream.time_base if vstream is not None else Fraction(1, 1)
        a_tb = astream.time_base if astream is not None else Fraction(1, 1)

        streams = [s for s in (vstream, astream) if s is not None]
        for packet in container.demux(streams):
            for frame in packet.decode():
                if vstream is not None and packet.stream is vstream:
                    cross_pts = video_det.feed(frame, v_tb)
                    if cross_pts is not None:
                        if stats is not None:
                            stats['n_crosses'] += 1
                            stats['crosses'].append(cross_pts)
                        silence_pts = (
                            audio_det.last_silence_pts_s if audio_det else None
                        )
                        ev = correlator.update(silence_pts, cross_pts, "cross")
                        if ev:
                            yield ev
                elif astream is not None and packet.stream is astream:
                    if audio_det is None:
                        audio_det = AudioSilenceDetector(cfg, int(frame.rate))
                    for s_pts in audio_det.feed(frame, a_tb):
                        if stats is not None:
                            stats['n_silences'] += 1
                            stats['silences'].append(s_pts)
                        cross_pts = (
                            video_det.last_cross_pts_s if video_det else None
                        )
                        ev = correlator.update(s_pts, cross_pts, "silence")
                        if ev:
                            yield ev


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _frame_pts_s(frame, fallback_time_base: Fraction) -> float:
    pts = frame.pts if frame.pts is not None else 0
    tb = frame.time_base if frame.time_base is not None else fallback_time_base
    return float(pts * tb)


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


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Measure A/V sync from a Vistek-encoded recording.",
    )
    p.add_argument("input", help="Path to recording (any container ffmpeg can demux).")
    p.add_argument("--csv", action="store_true",
                   help="Emit machine-readable CSV instead of human-readable lines.")
    p.add_argument("--audio-threshold-db", type=float, default=-50.0,
                   help="RMS dBFS below which audio is considered silent (default: -50).")
    p.add_argument("--pixel-black-max", type=int, default=39,
                   help="Per-channel RGB max to count a pixel as black (default: 39).")
    p.add_argument("--colorbar-white-min", type=int, default=242,
                   help="Per-channel RGB minimum to count the centre pixel as white (default: 242, i.e. 95%% of 255).")
    p.add_argument("--sample-x", type=int, default=10,
                   help="Column to sample for the black-cross test (default: 10).")
    p.add_argument("--audio-channel", type=int, default=0,
                   help="Audio channel index to analyze (default: 0 = CH1).")
    p.add_argument("--rms-window-ms", type=float, default=2.0,
                   help="RMS analysis window length in ms (default: 2.0).")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress the speed/summary line on stderr.")
    args = p.parse_args(argv)

    cfg = AnalyzerConfig(
        audio_threshold_db=args.audio_threshold_db,
        pixel_black_max=args.pixel_black_max,
        colorbar_white_min=args.colorbar_white_min,
        sample_x=args.sample_x,
        audio_channel=args.audio_channel,
        rms_window_ms=args.rms_window_ms,
    )

    count = 0
    delay_changes = 0
    last_delay_ms: Optional[float] = None
    last_pts_s = 0.0
    t_start = time.perf_counter()
    stats: dict = {}

    if args.csv:
        print("event_pts_s,av_delay_ms,silence_pts_s,cross_pts_s,trigger")

    for ev in analyze_file(args.input, cfg, stats=stats):
        count += 1
        last_pts_s = max(last_pts_s, ev.pts_s)
        if ev.delay_ms != last_delay_ms:
            delay_changes += 1
            last_delay_ms = ev.delay_ms
        if args.csv:
            print(f"{ev.pts_s:.3f},{ev.delay_ms:.1f},"
                  f"{ev.silence_pts_s:.3f},{ev.cross_pts_s:.3f},{ev.trigger}")
        else:
            sign = "+" if ev.delay_ms >= 0 else ""
            print(f"{_format_timestamp(ev.pts_s)}  "
                  f"A/V delay: {sign}{ev.delay_ms:7.1f} ms  "
                  f"(silence@{ev.silence_pts_s:.3f}s  cross@{ev.cross_pts_s:.3f}s  "
                  f"via {ev.trigger})")

    if not args.quiet:
        crosses = stats.get('crosses', [])
        silences = stats.get('silences', [])
        print(f"[vistek] video crosses  ({len(crosses)}): "
              + ", ".join(f"{t:.3f}s" for t in crosses), file=sys.stderr)
        print(f"[vistek] audio silences ({len(silences)}): "
              + ", ".join(f"{t:.3f}s" for t in silences), file=sys.stderr)

        elapsed = time.perf_counter() - t_start
        if last_pts_s > 0 and elapsed > 0:
            speed = last_pts_s / elapsed
            print(
                f"[vistek] processed {last_pts_s:.1f}s of media in {elapsed:.2f}s "
                f"({speed:.1f}x realtime, {count} detections, {delay_changes} delay changes)",
                file=sys.stderr,
            )
        else:
            n_c = stats.get('n_crosses', 0)
            n_s = stats.get('n_silences', 0)
            print(
                f"[vistek] no delay events emitted "
                f"({n_c} video cross(es), {n_s} audio silence(s) detected; "
                f"{elapsed:.2f}s wallclock)",
                file=sys.stderr,
            )

    return 0 if count > 0 else 2


if __name__ == "__main__":
    sys.exit(main())
