"""HLS live preview output — rolling window using PyAV's libav HLS muxer."""

from __future__ import annotations

import fractions
import logging
import time
from pathlib import Path
from typing import Optional

import av
import numpy as np

from ..config import OutputConfig
from . import path_template as pt
from .base import AudioPacket, OutputThread, VideoFrame
from .file_output import _downmix_stereo, _map_fmt, _parse_bitrate, _select_audio_channels

logger = logging.getLogger(__name__)


class HlsOutput(OutputThread):
    """Live HLS output with a rolling window of hls_list_size .ts segments."""

    def __init__(self, cfg: OutputConfig, channel_name: str):
        super().__init__(cfg.name, channel_name, cfg.segment_seconds)
        self.cfg = cfg

    def _encoder_loop(self) -> None:
        playlist_path = str(pt.ensure_parent(pt.render(
            self.cfg.path_template,
            output_name=self.cfg.name,
            channel_name=self.channel_name,
            start_unix_ms=int(time.time() * 1000),
        )))
        self._log.info("HLS playlist: %s", playlist_path)

        container: Optional[av.container.OutputContainer] = None
        vstream = None
        astream = None

        hls_opts = {
            "hls_time": str(self.cfg.segment_seconds),
            "hls_list_size": str(self.cfg.hls_list_size),
            "hls_flags": "delete_segments+append_list",
            "hls_segment_type": "mpegts",
        }

        try:
            while not self._stop_event.is_set():
                frame = self._get_video(timeout=1.0)
                if frame is None:
                    if self._stop_event.is_set():
                        break
                    continue

                # Lazy container/stream setup on first frame — we need width/height/framerate.
                if container is None:
                    fps_num, fps_den = frame.framerate if frame.framerate[1] else (frame.framerate[0], 1)
                    rate = fractions.Fraction(fps_num, fps_den) if fps_den else fractions.Fraction(fps_num, 1)
                    container = av.open(playlist_path, mode="w", format="hls", options=hls_opts)

                    vcfg = self.cfg.video
                    vstream = container.add_stream(vcfg.codec, rate=rate)
                    vstream.codec_context.width = frame.width
                    vstream.codec_context.height = frame.height
                    vstream.codec_context.pix_fmt = vcfg.pix_fmt
                    vstream.codec_context.options["preset"] = vcfg.preset
                    if vcfg.bitrate:
                        vstream.bit_rate = _parse_bitrate(vcfg.bitrate)
                    if vcfg.options:
                        vstream.codec_context.options.update({k: str(v) for k, v in vcfg.options.items()})
                    # HLS cuts at keyframe boundaries, so force GOP = segment_seconds * fps.
                    # Without this, nvenc's default ~250-frame GOP produces ~10s segments
                    # regardless of hls_time. User can override via vcfg.options["g"].
                    if "g" not in vstream.codec_context.options:
                        gop_frames = max(1, round(float(rate) * self.cfg.segment_seconds))
                        vstream.codec_context.options["g"] = str(gop_frames)

                    acfg = self.cfg.audio
                    out_channels = 2 if acfg.downmix == "stereo" else len(acfg.channels)
                    astream = container.add_stream(acfg.codec, rate=48000, layout="stereo" if out_channels == 2 else f"{out_channels}c")
                    if acfg.bitrate:
                        astream.bit_rate = _parse_bitrate(acfg.bitrate)

                    self._log.info("HLS streams opened (size=%dx%d rate=%s codec=%s)",
                                   frame.width, frame.height, rate, vcfg.codec)

                try:
                    av_frame = av.VideoFrame.from_ndarray(frame.data, format=_map_fmt(frame.fmt))
                    av_frame.pts = self.stats.frames_written
                    pkt_count = 0
                    for pkt in vstream.encode(av_frame):
                        container.mux(pkt)
                        pkt_count += 1
                    self.video_pkts_muxed += pkt_count
                    if self.stats.frames_written == 0:
                        self._log.info("First HLS video frame encoded (produced %d packet(s))", pkt_count)
                    self.stats.frames_written += 1
                except Exception as e:
                    self._log.error("HLS video encode error: %s", e, exc_info=True)

                for apkt in self._drain_audio():
                    try:
                        audio_data = _select_audio_channels(apkt.data, apkt.channels, self.cfg.audio.channels)
                        if self.cfg.audio.downmix == "stereo":
                            audio_data = _downmix_stereo(audio_data)
                        layout = "stereo" if audio_data.shape[1] == 2 else f"{audio_data.shape[1]}c"
                        # int16 → int32 in upper bits: s32 codec treats full ±2^31 as the
                        # audio range, so we left-shift int16 into the high bits to keep
                        # amplitude correct.
                        arr = (audio_data.astype(np.int32) << 16).reshape(1, -1)
                        af = av.AudioFrame.from_ndarray(arr, format="s32", layout=layout)
                        af.sample_rate = apkt.sample_rate
                        if self.audio_frames_encoded == 0:
                            self._log.info(
                                "First HLS audio frame: src_ch=%d sel_ch=%d downmix=%s layout=%s "
                                "samples=%d sr=%d codec=%s codec_fmt=%s",
                                apkt.channels, audio_data.shape[1], self.cfg.audio.downmix,
                                layout, audio_data.shape[0], apkt.sample_rate,
                                self.cfg.audio.codec,
                                astream.codec_context.format,
                            )
                        pkt_count = 0
                        for pkt in astream.encode(af):
                            container.mux(pkt)
                            pkt_count += 1
                        self.audio_pkts_muxed += pkt_count
                        if self.audio_frames_encoded == 0:
                            self._log.info(
                                "First HLS audio frame encoded (produced %d packet(s))", pkt_count
                            )
                        self.audio_frames_encoded += 1
                    except Exception as e:
                        self._log.error("HLS audio encode error: %s", e, exc_info=True)

        finally:
            if container:
                try:
                    if vstream:
                        for pkt in vstream.encode(None):
                            container.mux(pkt)
                    if astream:
                        for pkt in astream.encode(None):
                            container.mux(pkt)
                    container.close()
                except Exception as e:
                    self._log.error("HLS flush error: %s", e)
