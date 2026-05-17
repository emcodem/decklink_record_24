"""YAML config loader and dataclasses."""

from __future__ import annotations

import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class VideoEncoderConfig:
    codec: str = "hevc_nvenc"
    preset: str = "p4"
    bitrate: str = "8M"
    pix_fmt: str = "yuv420p"
    profile: Optional[str] = None
    options: dict = field(default_factory=dict)   # extra PyAV codec options


@dataclass
class AudioEncoderConfig:
    channels: list[int] = field(default_factory=lambda: [1, 2])   # 1-based SDI channels
    downmix: str = "none"             # "stereo" | "none"
    track_mode: str = "combined"      # "combined" | "mono_per_channel"
    codec: str = "aac"
    bitrate: Optional[str] = "128k"


@dataclass
class OutputConfig:
    name: str
    type: str                # "file" | "hls"
    path_template: str
    segment_seconds: int = 600
    enabled: bool = True
    # HLS-specific
    hls_list_size: int = 2
    # Per-output filter chains (applied after the channel-level capture.video_filter).
    # Same ffmpeg -vf / -af syntax accepted by capture.video_filter.
    # Example: video_filter: "scale=640:-2"
    video_filter: str = ""
    audio_filter: str = ""
    # Sub-configs
    video: VideoEncoderConfig = field(default_factory=VideoEncoderConfig)
    audio: AudioEncoderConfig = field(default_factory=AudioEncoderConfig)


@dataclass
class CaptureConfig:
    audio_channels: int = 8
    pix_fmt: str = "yuv420p"     # target pixel format for the filter chain's output (auto-appended)
    # Full ffmpeg-style filter chain, comma-separated. When empty, no filter graph is
    # built and the raw uyvy422 frames are passed through unchanged.
    # The filter graph's actual output width/height/framerate/pix_fmt are queried at
    # build time and propagated to the encoder — so a `bwdif=mode=1` doubling 25p→50p
    # results in the encoder being initialized at 50 fps.
    # Examples:
    #   video_filter: "yadif=mode=0:parity=auto:deint=interlaced,format=yuv420p"  # 25i → 25p
    #   video_filter: "bwdif=mode=1:parity=-1:deint=0,format=yuv420p"             # 25i → 50p
    #   video_filter: "scale=1280:720:flags=lanczos,format=yuv420p"               # downscale
    # Commas inside filter args are supported via single-quoted regions
    # (text='hello, world') or backslash escapes (text=hello\, world), so most
    # working ffmpeg `-vf` strings can be pasted verbatim.
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


def _parse_video(d: dict) -> VideoEncoderConfig:
    return VideoEncoderConfig(
        codec=d.get("codec", "hevc_nvenc"),
        preset=d.get("preset", "p4"),
        bitrate=d.get("bitrate", "8M"),
        pix_fmt=d.get("pix_fmt", "yuv420p"),
        profile=d.get("profile"),
        options=d.get("options", {}),
    )


def _parse_audio(d: dict) -> AudioEncoderConfig:
    return AudioEncoderConfig(
        channels=d.get("channels", [1, 2]),
        downmix=d.get("downmix", "none"),
        track_mode=d.get("track_mode", "combined"),
        codec=d.get("codec", "aac"),
        bitrate=d.get("bitrate", "128k"),
    )


def _parse_output(d: dict) -> OutputConfig:
    return OutputConfig(
        name=d["name"],
        type=d["type"],
        path_template=d["path_template"],
        segment_seconds=d.get("segment_seconds", 600),
        enabled=d.get("enabled", True),
        hls_list_size=d.get("hls_list_size", 2),
        video_filter=d.get("video_filter", ""),
        audio_filter=d.get("audio_filter", ""),
        video=_parse_video(d.get("video", {})),
        audio=_parse_audio(d.get("audio", {})),
    )


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
        outputs=[_parse_output(o) for o in raw.get("outputs", [])],
    )
