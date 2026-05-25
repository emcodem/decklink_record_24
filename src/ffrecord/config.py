"""YAML config loader and dataclasses.

The output schema is **container-agnostic**: an output describes which libav
container format to write, which options to pass to it, and whether the app
manages segmentation (close+reopen per N seconds, used by the archive case
where each file must be independently playable) or leaves that to libav (used
by HLS, DASH, the segment muxer, anything that splits internally).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

import yaml

logger = logging.getLogger("ffrecord.config")


@dataclass
class VideoEncoderConfig:
    codec: str = "hevc_nvenc"
    preset: str = "p4"
    bitrate: str = "8M"
    pix_fmt: str = "yuv420p"
    profile: Optional[str] = None
    options: dict = field(default_factory=dict)   # extra PyAV codec options
    # GOP / keyframe interval:
    #   "auto"        — leave to codec defaults
    #   int N         — set "g=N" (frames)
    #   "seconds:N"   — compute g = round(N * fps) at start time
    gop: Union[str, int] = "auto"


@dataclass
class AudioEncoderConfig:
    channels: list[int] = field(default_factory=lambda: [1, 2])   # 1-based SDI channels
    downmix: str = "none"             # "stereo" | "none"
    track_mode: str = "combined"      # "combined" | "mono_per_channel"
    codec: str = "aac"
    bitrate: Optional[str] = "128k"


@dataclass
class InternalSplitterConfig:
    """App-managed segmentation: close+reopen the container every `seconds` of
    captured video frames. Use this for archive outputs where each segment is
    expected to be an independent, frame-0-starting file. Leave disabled for
    HLS / DASH / segment-muxer outputs where libav slices internally.
    """
    enabled: bool = False
    seconds: int = 600


@dataclass
class OutputConfig:
    name: str
    path_template: str
    # libav format string: "mov", "mp4", "mxf", "matroska", "hls", "dash", ...
    container_format: str = "mov"
    container_options: dict = field(default_factory=dict)
    internal_splitter: InternalSplitterConfig = field(default_factory=InternalSplitterConfig)
    enabled: bool = True
    # Per-output filter chains (applied after the channel-level capture.video_filter).
    # Same ffmpeg -vf / -af syntax accepted by capture.video_filter.
    video_filter: str = ""
    audio_filter: str = ""
    video: VideoEncoderConfig = field(default_factory=VideoEncoderConfig)
    audio: AudioEncoderConfig = field(default_factory=AudioEncoderConfig)


@dataclass
class CaptureConfig:
    audio_channels: int = 8
    pix_fmt: str = "yuv420p"     # target pixel format for the filter chain's output (auto-appended)
    video_filter: str = ""


@dataclass
class ChannelConfig:
    name: str = "CH1"
    decklink_device_index: int = 0
    expected_format: str = "bmdModeHD1080i50"


@dataclass
class HttpConfig:
    bind: str = "127.0.0.1"
    port: int = 8081


@dataclass
class LoggingConfig:
    dir: str = "logs"
    file_rotation_days: int = 7
    level: str = "INFO"


@dataclass
class ServiceConfig:
    channel: ChannelConfig = field(default_factory=ChannelConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    http: HttpConfig = field(default_factory=HttpConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    outputs: list[OutputConfig] = field(default_factory=list)


# ── parsing ────────────────────────────────────────────────────────────────


def _parse_video(d: dict) -> VideoEncoderConfig:
    return VideoEncoderConfig(
        codec=d.get("codec", "hevc_nvenc"),
        preset=d.get("preset", "p4"),
        bitrate=d.get("bitrate", "8M"),
        pix_fmt=d.get("pix_fmt", "yuv420p"),
        profile=d.get("profile"),
        options=d.get("options", {}),
        gop=d.get("gop", "auto"),
    )


def _parse_audio(d: dict) -> AudioEncoderConfig:
    return AudioEncoderConfig(
        channels=d.get("channels", [1, 2]),
        downmix=d.get("downmix", "none"),
        track_mode=d.get("track_mode", "combined"),
        codec=d.get("codec", "aac"),
        bitrate=d.get("bitrate", "128k"),
    )


def _parse_internal_splitter(d: dict) -> InternalSplitterConfig:
    return InternalSplitterConfig(
        enabled=bool(d.get("enabled", False)),
        seconds=int(d.get("seconds", 600)),
    )


# Output keys that were valid in the pre-unification schema. Their presence
# now indicates an un-migrated YAML — we refuse to start rather than silently
# fall back to defaults that may differ from the operator's intent.
_LEGACY_OUTPUT_FIELDS = ("type", "segment_seconds", "hls_list_size")


def _parse_output(d: dict, output_index: int) -> OutputConfig:
    name = d.get("name", f"output{output_index}")

    legacy_present = [k for k in _LEGACY_OUTPUT_FIELDS if k in d]
    if legacy_present:
        raise ValueError(
            f"Output '{name}' uses removed legacy schema fields: "
            f"{', '.join(legacy_present)}. Migrate to container_format + "
            f"container_options + internal_splitter (see config/example.yaml)."
        )

    out = OutputConfig(
        name=name,
        path_template=d["path_template"],
        container_format=d.get("container_format", "mov"),
        container_options=d.get("container_options", {}) or {},
        internal_splitter=_parse_internal_splitter(d.get("internal_splitter", {})),
        enabled=d.get("enabled", True),
        video_filter=d.get("video_filter", ""),
        audio_filter=d.get("audio_filter", ""),
        video=_parse_video(d.get("video", {})),
        audio=_parse_audio(d.get("audio", {})),
    )

    _validate_output(out)
    return out


def _validate_output(out: OutputConfig) -> None:
    """Fail loudly at load time for misconfigurations that would only surface
    later as cryptic libav errors or silent feature drops.
    """
    errors: list[str] = []

    if out.container_format == "hls" and out.audio.track_mode == "mono_per_channel":
        errors.append(
            "container_format='hls' is incompatible with audio.track_mode="
            "'mono_per_channel' (libav's HLS muxer cannot carry N parallel "
            "mono streams). Use 'combined' for HLS."
        )

    if out.internal_splitter.enabled and out.container_format == "hls":
        errors.append(
            "internal_splitter.enabled=true is incompatible with "
            "container_format='hls' (libav's HLS muxer handles segmentation "
            "itself; app-managed splitting would close the playlist mid-stream)."
        )

    if out.internal_splitter.enabled and out.internal_splitter.seconds <= 0:
        errors.append(
            "internal_splitter.enabled=true requires internal_splitter.seconds > 0; "
            f"got {out.internal_splitter.seconds}."
        )

    gop = out.video.gop
    if isinstance(gop, str) and gop.startswith("seconds:"):
        try:
            seconds = float(gop.split(":", 1)[1])
            if seconds <= 0:
                errors.append(f"video.gop='{gop}' must specify a positive number of seconds.")
        except (ValueError, IndexError):
            errors.append(f"video.gop='{gop}' is malformed; expected 'seconds:N' with N>0.")

    if errors:
        msg = f"Invalid output config '{out.name}':\n  - " + "\n  - ".join(errors)
        raise ValueError(msg)


def load_config(path: str | Path) -> ServiceConfig:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    ch = raw.get("channel", {})
    cap = raw.get("capture", {})
    http = raw.get("http", {})
    log = raw.get("logging", {})

    return ServiceConfig(
        channel=ChannelConfig(
            name=ch.get("name", "CH1"),
            decklink_device_index=ch.get("decklink_device_index", 0),
            expected_format=ch.get("expected_format", "bmdModeHD1080i50"),
        ),
        capture=CaptureConfig(
            audio_channels=cap.get("audio_channels", 8),
            pix_fmt=cap.get("pix_fmt", "yuv420p"),
            video_filter=cap.get("video_filter", ""),
        ),
        http=HttpConfig(
            bind=http.get("bind", "127.0.0.1"),
            port=http.get("port", 8081),
        ),
        logging=LoggingConfig(
            dir=log.get("dir", "logs"),
            file_rotation_days=log.get("file_rotation_days", 7),
            level=log.get("level", "INFO"),
        ),
        outputs=[_parse_output(o, i) for i, o in enumerate(raw.get("outputs", []))],
    )
