"""Output-side helpers shared across all encoder outputs.

Everything in here is container-format-agnostic. Anything that needs to know
"is this MOV vs HLS?" belongs in the caller, not here.
"""

from __future__ import annotations

import fractions
import logging
from typing import Optional

import av
import numpy as np

from ..config import AudioEncoderConfig, OutputConfig, VideoEncoderConfig
from ..sync_log import log_mux_failure

logger = logging.getLogger(__name__)


# ── parsing helpers ────────────────────────────────────────────────────────


def parse_bitrate(s: str) -> int:
    """Parse '8M', '128k', '500000' into bits per second.

    Returns 0 if the string is empty or unparseable. Caller decides what to do
    with 0 (typically: leave libav at its codec default).
    """
    if not s:
        return 0
    s = s.strip().upper()
    try:
        if s.endswith("M"):
            return int(float(s[:-1]) * 1_000_000)
        if s.endswith("K"):
            return int(float(s[:-1]) * 1_000)
        if s.endswith("G"):
            return int(float(s[:-1]) * 1_000_000_000)
        return int(s)
    except (ValueError, TypeError) as e:
        logger.warning("Bitrate parse failed for %r: %s — falling back to codec default", s, e)
        return 0


def map_pix_fmt(fmt: str) -> str:
    """Normalize pixel format names to libav's lowercase canon."""
    mapping = {
        "uyvy422": "uyvy422",
        "RGB24": "rgb24",
        "rgb24": "rgb24",
        "YUV420P": "yuv420p",
        "yuv420p": "yuv420p",
    }
    return mapping.get(fmt, fmt.lower())


# ── audio channel utilities ────────────────────────────────────────────────


def select_audio_channels(data: np.ndarray, src_channels: int, channel_list: list[int]) -> np.ndarray:
    """Pick 1-based SDI channel indices from a multi-channel int16 audio buffer.

    Returns the first channel as a fallback when the channel_list is empty or
    out-of-range — encoders dislike zero-channel inputs and an unexpectedly
    silent first channel is easier to diagnose than a downstream crash.
    """
    indices = [c - 1 for c in channel_list if 0 < c <= src_channels]
    if not indices:
        return data[:, :1]
    return data[:, indices]


def downmix_stereo(data: np.ndarray) -> np.ndarray:
    """Average input channels into duplicated mono pair (2-ch int16)."""
    if data.shape[1] == 2:
        return data
    mono = data.mean(axis=1, keepdims=True).astype(np.int16)
    return np.concatenate([mono, mono], axis=1)


# ── container / stream setup ───────────────────────────────────────────────


def open_container(cfg: OutputConfig, path: str) -> av.container.OutputContainer:
    """Open a libav container for writing using the output's config.

    Passes cfg.container_format as the libav format string and cfg.container_options
    as options. The format string drives muxer selection (mov, hls, mpegts, ...);
    the options dict drives muxer-specific behaviour (hls_time, hls_list_size,
    movflags, ...).
    """
    fmt = cfg.container_format or None
    options = {k: str(v) for k, v in (cfg.container_options or {}).items()}
    return av.open(path, mode="w", format=fmt, options=options or None)


def resolve_gop(vcfg: VideoEncoderConfig, framerate: tuple[int, int]) -> Optional[int]:
    """Translate cfg.video.gop to an explicit GOP-frames count.

    Returns:
        None    — caller should leave the codec at its default
        int     — caller should set codec_context.options["g"] = str(N)

    Accepted values for vcfg.gop:
        "auto"       — None
        int N        — N (if positive)
        "seconds:N"  — round(N * fps), minimum 1
    """
    g = vcfg.gop
    if isinstance(g, int):
        return g if g > 0 else None
    if isinstance(g, str):
        s = g.strip().lower()
        if s in ("", "auto"):
            return None
        if s.startswith("seconds:"):
            try:
                seconds = float(s.split(":", 1)[1])
            except (ValueError, IndexError):
                logger.warning("video.gop=%r unparseable — using codec default", g)
                return None
            fps_num, fps_den = framerate if framerate[1] else (framerate[0], 1)
            if fps_num <= 0 or fps_den <= 0:
                return None
            fps = fps_num / fps_den
            return max(1, round(seconds * fps))
        try:
            n = int(s)
            return n if n > 0 else None
        except ValueError:
            logger.warning("video.gop=%r unparseable — using codec default", g)
            return None
    return None


def build_video_stream(
    container: av.container.OutputContainer,
    vcfg: VideoEncoderConfig,
    width: int,
    height: int,
    framerate: tuple[int, int],
    pix_fmt: Optional[str] = None,
):
    """Configure a video stream on the open container per the encoder config."""
    fps_num, fps_den = framerate if framerate[1] else (framerate[0], 1)
    rate = fractions.Fraction(fps_num, fps_den) if fps_den else fractions.Fraction(fps_num, 1)
    stream = container.add_stream(vcfg.codec, rate=rate)
    stream.codec_context.framerate = rate
    stream.codec_context.time_base = fractions.Fraction(fps_den, fps_num)

    codec_opts: dict[str, str] = {}
    if vcfg.profile:
        codec_opts["profile"] = vcfg.profile
    if vcfg.options:
        codec_opts.update({k: str(v) for k, v in vcfg.options.items()})
    if vcfg.preset:
        codec_opts["preset"] = vcfg.preset
    gop_frames = resolve_gop(vcfg, framerate)
    if gop_frames is not None and "g" not in codec_opts:
        codec_opts["g"] = str(gop_frames)

    if codec_opts:
        # `stream.options` is the dict checked on add_stream; some libav versions
        # also need it on codec_context.options. Set both for safety.
        stream.options = dict(codec_opts)
        stream.codec_context.options.update(codec_opts)

    stream.codec_context.width = width
    stream.codec_context.height = height
    stream.codec_context.pix_fmt = pix_fmt or vcfg.pix_fmt

    br = parse_bitrate(vcfg.bitrate)
    if br:
        stream.bit_rate = br

    return stream, rate, gop_frames


def build_audio_streams(
    container: av.container.OutputContainer,
    acfg: AudioEncoderConfig,
) -> list:
    """Build the audio stream list per the encoder config.

    Returns a list of streams. Length depends on track_mode:
        - "combined"          — one N-channel stream
        - "mono_per_channel"  — N mono streams (one per selected SDI channel)

    If downmix=="stereo" the count is forced to 2 ("combined" only).
    """
    if acfg.downmix == "stereo":
        out_channels = 2
    else:
        out_channels = len(acfg.channels)

    streams: list = []
    br = parse_bitrate(acfg.bitrate) if acfg.bitrate else 0

    if acfg.track_mode == "mono_per_channel" and acfg.downmix != "stereo":
        for _ in range(out_channels):
            s = container.add_stream(acfg.codec, rate=48000, layout="mono")
            if br:
                s.bit_rate = br
            streams.append(s)
    else:
        layout = "stereo" if out_channels == 2 else f"{out_channels}c"
        s = container.add_stream(acfg.codec, rate=48000, layout=layout)
        if br:
            s.bit_rate = br
        streams.append(s)
    return streams


# ── mux with logging ───────────────────────────────────────────────────────


class MuxCounters:
    """Per-output mux counters. Owned by EncoderOutput so log_mux_failure has
    a stable place to read the total from.
    """

    __slots__ = ("video_failures", "audio_failures", "total_failures")

    def __init__(self) -> None:
        self.video_failures = 0
        self.audio_failures = 0
        self.total_failures = 0


def _ffmpeg_error_detail(e: Exception) -> str:
    """Pull libav's descriptive message + errno off a PyAV exception.

    PyAV attaches the last av_log line to the exception as
    e.log = (level:int, name:str, message:str). The bare str(e) is often just
    "Invalid argument: '...' returned 22" — the real reason lives in e.log.
    """
    bits = [f"{type(e).__name__}: {e}"]
    log = getattr(e, "log", None)
    if log:
        try:
            _level, name, message = log
            bits.append(f"libav[{name}]: {message.strip()}")
        except Exception:
            bits.append(f"libav_log={log!r}")
    errno = getattr(e, "errno", None)
    if errno is not None:
        bits.append(f"errno={errno}")
    return " | ".join(bits)


def mux_with_logging(
    container,
    packet,
    output_name: str,
    counters: MuxCounters,
    *,
    kind: str = "video",
) -> bool:
    """Wrap container.mux(packet). On failure: log + count, do not re-raise.

    The encoder loop must keep running through transient mux failures
    (broadcast = best-effort). A counter exposed in /status lets operators see
    accumulated failures even when the rest of the pipeline appears healthy.
    """
    try:
        container.mux(packet)
        return True
    except Exception as e:
        counters.total_failures += 1
        if kind == "audio":
            counters.audio_failures += 1
        else:
            counters.video_failures += 1
        detail = _ffmpeg_error_detail(e)
        logger.warning(
            "[mux] FAILED output=%s kind=%s %s",
            output_name, kind, detail,
        )
        log_mux_failure(output_name, f"{kind}: {detail}", counters.total_failures)
        return False
