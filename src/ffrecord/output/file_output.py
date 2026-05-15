"""Segmented file output (MOV / MP4 / MXF) using PyAV."""

from __future__ import annotations

import fractions
import logging
import queue
import time
from typing import Optional

import av
import numpy as np

from ..config import OutputConfig
from . import path_template as pt
from .base import AudioPacket, OutputThread, VideoFrame

logger = logging.getLogger(__name__)


def _select_audio_channels(data: np.ndarray, src_channels: int, channel_list: list[int]) -> np.ndarray:
    """Select (and optionally downmix) audio channels.

    channel_list: 1-based SDI channel indices to include.
    Returns (samples, out_channels) int16.
    """
    # Clamp to available channels
    indices = [c - 1 for c in channel_list if 0 < c <= src_channels]
    if not indices:
        return data[:, :1]
    selected = data[:, indices]
    return selected


def _downmix_stereo(data: np.ndarray) -> np.ndarray:
    """Average all input channels to stereo (2-ch) int16."""
    if data.shape[1] == 2:
        return data
    mono = data.mean(axis=1, keepdims=True).astype(np.int16)
    return np.concatenate([mono, mono], axis=1)


class FileOutput(OutputThread):
    """Records to segmented MOV/MP4/MXF files using PyAV with NVENC."""

    def __init__(self, cfg: OutputConfig, channel_name: str):
        super().__init__(cfg.name, channel_name, cfg.segment_seconds)
        self.cfg = cfg

    def _encoder_loop(self) -> None:
        segment_seq = self.stats.segments_completed
        segment_start_ms: Optional[int] = None
        container: Optional[av.container.OutputContainer] = None
        vstream = None
        astreams: list = []
        # Per-segment PTS counters. Each new segment is its own MOV file with a
        # local timeline starting at 0, so PTS must reset on open — otherwise
        # segment N's video starts at pts = N×(frames_per_segment), which the MOV
        # muxer treats as a several-second offset and the audio (which auto-PTSes
        # to 0) plays only over that prefix gap.
        seg_v_pts = 0
        seg_a_pts_samples = 0
        seg_a_frames = 0
        frames_per_segment = 0
        _post_format_change = False

        def open_new_segment(start_ms: int, width: int, height: int,
                             framerate: tuple[int, int]) -> None:
            nonlocal container, vstream, astreams
            nonlocal seg_v_pts, seg_a_pts_samples, seg_a_frames, frames_per_segment
            path = pt.render(
                self.cfg.path_template,
                output_name=self.cfg.name,
                channel_name=self.channel_name,
                start_unix_ms=start_ms,
                seq=self.stats.segments_completed,
            )
            abs_path = pt.ensure_parent(path)
            fps_num, fps_den = framerate if framerate[1] else (framerate[0], 1)
            rate = fractions.Fraction(fps_num, fps_den) if fps_den else fractions.Fraction(fps_num, 1)
            self._log.info("Opening segment: %s (size=%dx%d rate=%s)", abs_path, width, height, rate)

            container = av.open(str(abs_path), mode="w")
            vcfg = self.cfg.video

            codec_opts = {}
            if vcfg.profile:
                codec_opts["profile"] = vcfg.profile
            codec_opts.update(vcfg.options)

            vstream = container.add_stream(vcfg.codec, rate=rate)
            vstream.options = codec_opts
            if vcfg.bitrate:
                vstream.bit_rate = _parse_bitrate(vcfg.bitrate)
            vstream.codec_context.width = width
            vstream.codec_context.height = height
            vstream.codec_context.pix_fmt = vcfg.pix_fmt
            vstream.codec_context.options["preset"] = vcfg.preset

            acfg = self.cfg.audio
            if acfg.downmix == "stereo":
                out_channels = 2
            else:
                out_channels = len(acfg.channels)

            astreams = []
            if acfg.track_mode == "mono_per_channel" and acfg.downmix != "stereo":
                for _ in range(out_channels):
                    s = container.add_stream(acfg.codec, rate=48000, layout="mono")
                    if acfg.bitrate:
                        s.bit_rate = _parse_bitrate(acfg.bitrate)
                    astreams.append(s)
            else:
                a_layout = "stereo" if out_channels == 2 else f"{out_channels}c"
                s = container.add_stream(acfg.codec, rate=48000, layout=a_layout)
                if acfg.bitrate:
                    s.bit_rate = _parse_bitrate(acfg.bitrate)
                astreams.append(s)

            self._log.info(
                "Audio streams added: track_mode=%s count=%d codec=%s sample_rate=48000 codec_fmt=%s",
                acfg.track_mode, len(astreams), acfg.codec,
                astreams[0].codec_context.format,
            )

            seg_v_pts = 0
            seg_a_pts_samples = 0
            seg_a_frames = 0
            frames_per_segment = round(self.cfg.segment_seconds * float(rate))
            self._log.info("Segment opened (seq=%d start_ms=%d v_codec=%s pix_fmt=%s preset=%s a_codec=%s)",
                           self.stats.segments_completed, start_ms,
                           vcfg.codec, vcfg.pix_fmt, vcfg.preset, acfg.codec)

        def close_segment() -> None:
            nonlocal container, vstream, astreams
            if container is None:
                return
            try:
                if vstream:
                    for pkt in vstream.encode(None):
                        container.mux(pkt)
                for s in astreams:
                    for pkt in s.encode(None):
                        container.mux(pkt)
                container.close()
                self.stats.segments_completed += 1
                self._log.info(
                    "Segment closed (total segments=%d, this seg: v_frames=%d a_frames=%d a_samples=%d)",
                    self.stats.segments_completed, seg_v_pts, seg_a_frames, seg_a_pts_samples,
                )
            except Exception as e:
                self._log.error("Error closing segment: %s", e, exc_info=True)
            finally:
                container = None
                vstream = None
                astreams = []

        try:
            while not self._stop_event.is_set():
                frame = self._get_video(timeout=1.0)
                if frame is None:
                    # Timeout or stop sentinel
                    if self._stop_event.is_set():
                        break
                    continue

                now_ms = int(time.time() * 1000)

                if container is None:
                    segment_start_ms = now_ms
                    open_new_segment(segment_start_ms, frame.width, frame.height, frame.framerate)

                # Segment rollover by frame count — deterministic, exact duration.
                if seg_v_pts >= frames_per_segment:
                    close_segment()
                    segment_start_ms = now_ms
                    open_new_segment(segment_start_ms, frame.width, frame.height, frame.framerate)

                # A/V alignment on format change: flush stale pre-change audio and wait for first post-change packet
                if self._format_change_pending.is_set():
                    self._format_change_pending.clear()
                    stale = self._drain_audio()
                    if stale:
                        self._log.info("[av_align] Flushed %d stale audio packet(s) after format change", len(stale))
                    _post_format_change = True

                if _post_format_change:
                    try:
                        first_audio = self._audio_queue.get(timeout=0.2)
                        self._audio_queue.put_nowait(first_audio)
                        self._log.info(
                            "[av_align] First post-change audio arrived (hw_pts=%d valid=%s); encoding aligned pair",
                            first_audio.hw_pts, first_audio.hw_pts_valid,
                        )
                    except queue.Empty:
                        self._log.warning("[av_align] Timeout waiting for post-change audio; proceeding unaligned")
                    _post_format_change = False

                # Encode video — pts is segment-local so each new MOV starts at 0.
                try:
                    av_frame = av.VideoFrame.from_ndarray(frame.data, format=_map_fmt(frame.fmt))
                    av_frame.pts = seg_v_pts
                    pkt_count = 0
                    for pkt in vstream.encode(av_frame):
                        container.mux(pkt)
                        pkt_count += 1
                    self.video_pkts_muxed += pkt_count
                    if self.stats.frames_written == 0:
                        self._log.info("First video frame encoded (produced %d packet(s))", pkt_count)
                    seg_v_pts += 1
                    self.stats.frames_written += 1
                except Exception as e:
                    self._log.error("Video encode error: %s", e, exc_info=True)

                # Encode any pending audio — pts is in sample-units, segment-local.
                for apkt in self._drain_audio():
                    try:
                        audio_data = _select_audio_channels(
                            apkt.data, apkt.channels, self.cfg.audio.channels
                        )
                        if self.cfg.audio.downmix == "stereo":
                            audio_data = _downmix_stereo(audio_data)

                        # int16 → int32 in upper bits: s32 codec treats full ±2^31 as the
                        # audio range, so we left-shift int16 into the high bits to keep
                        # amplitude correct. Without the shift, pcm_s24le and aac both
                        # produce ~96 dB-below-FS silence from valid int16 input.
                        pkt_count = 0
                        if len(astreams) == 1:
                            layout = "stereo" if audio_data.shape[1] == 2 else f"{audio_data.shape[1]}c"
                            arr = (audio_data.astype(np.int32) << 16).reshape(1, -1)
                            af = av.AudioFrame.from_ndarray(arr, format="s32", layout=layout)
                            af.sample_rate = apkt.sample_rate
                            af.pts = seg_a_pts_samples
                            af.time_base = fractions.Fraction(1, apkt.sample_rate)
                            for pkt in astreams[0].encode(af):
                                container.mux(pkt)
                                pkt_count += 1
                        else:
                            # mono_per_channel — one mono frame per stream
                            for ch_idx, astream in enumerate(astreams):
                                ch_data = audio_data[:, ch_idx:ch_idx + 1]
                                arr = (ch_data.astype(np.int32) << 16).reshape(1, -1)
                                af = av.AudioFrame.from_ndarray(arr, format="s32", layout="mono")
                                af.sample_rate = apkt.sample_rate
                                af.pts = seg_a_pts_samples
                                af.time_base = fractions.Fraction(1, apkt.sample_rate)
                                for pkt in astream.encode(af):
                                    container.mux(pkt)
                                    pkt_count += 1

                        if self.audio_frames_encoded == 0:
                            self._log.info(
                                "First audio frame: src_ch=%d sel_ch=%d downmix=%s "
                                "track_mode=%s streams=%d samples=%d sr=%d codec=%s",
                                apkt.channels, audio_data.shape[1], self.cfg.audio.downmix,
                                self.cfg.audio.track_mode, len(astreams),
                                audio_data.shape[0], apkt.sample_rate, self.cfg.audio.codec,
                            )
                            self._log.info(
                                "First audio frame encoded (produced %d packet(s))", pkt_count
                            )
                        self.audio_pkts_muxed += pkt_count
                        seg_a_pts_samples += audio_data.shape[0]
                        seg_a_frames += 1
                        self.audio_frames_encoded += 1
                    except Exception as e:
                        self._log.error("Audio encode error: %s", e, exc_info=True)

        finally:
            close_segment()


def _parse_bitrate(s: str) -> int:
    s = s.strip().upper()
    if s.endswith("M"):
        return int(float(s[:-1]) * 1_000_000)
    if s.endswith("K"):
        return int(float(s[:-1]) * 1_000)
    return int(s)


def _map_fmt(fmt: str) -> str:
    mapping = {
        "uyvy422": "uyvy422",
        "RGB24": "rgb24",
        "rgb24": "rgb24",
        "YUV420P": "yuv420p",
        "yuv420p": "yuv420p",
    }
    return mapping.get(fmt, fmt.lower())
