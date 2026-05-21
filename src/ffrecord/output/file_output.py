"""Segmented file output (MOV / MP4 / MXF) using PyAV.

Consumes pre-paired AVPair objects (one video frame + its matching audio
samples) produced by CaptureBuffer. Each segment opens a fresh container
with zero-based PTS counters; rollover is by frame count (deterministic
30-second segments). Because every pair carries exactly frame-sized audio,
segment video and audio durations are mathematically identical — no more
queue-depth-driven A/V drift.
"""

from __future__ import annotations

import fractions
import logging
import time
from typing import Optional

import av
import numpy as np

from ..config import OutputConfig
from . import path_template as pt
from .base import AVPair, OutputThread
from .output_filter import OutputAudioFilter, OutputVideoFilter

logger = logging.getLogger(__name__)


# ── interlace filter (shared with hls_output) ─────────────────────────────


def _make_setfield_filter(
    width: int, height: int, pix_fmt: str,
    fps_num: int, fps_den: int, top_field_first: bool,
):
    """Return (src, sink) of a buffer→setfield→buffersink graph.

    PyAV does not allow assigning interlaced_frame/top_field_first on
    VideoFrame objects in this version (read-only Cython properties).
    Routing each encoder input frame through this graph is the only way
    to embed the metadata so mpeg2video (and similar) write the correct
    field-order flag in the bitstream.
    """
    graph = av.filter.Graph()
    src = graph.add(
        "buffer",
        f"video_size={width}x{height}:"
        f"pix_fmt={pix_fmt}:"
        f"time_base={fps_den}/{fps_num}:"
        f"frame_rate={fps_num}/{fps_den}:"
        f"pixel_aspect=1/1",
    )
    sf = graph.add("setfield", "tff" if top_field_first else "bff")
    sink = graph.add("buffersink")
    src.link_to(sf)
    sf.link_to(sink)
    graph.configure()
    return src, sink


# ── audio channel utilities (shared with hls_output) ──────────────────────


def _select_audio_channels(data: np.ndarray, src_channels: int, channel_list: list[int]) -> np.ndarray:
    """Select 1-based SDI channel indices from a multi-channel audio buffer."""
    indices = [c - 1 for c in channel_list if 0 < c <= src_channels]
    if not indices:
        return data[:, :1]
    return data[:, indices]


def _downmix_stereo(data: np.ndarray) -> np.ndarray:
    """Average all input channels into a duplicated mono pair (2-ch int16)."""
    if data.shape[1] == 2:
        return data
    mono = data.mean(axis=1, keepdims=True).astype(np.int16)
    return np.concatenate([mono, mono], axis=1)


# ── output class ─────────────────────────────────────────────────────────


class FileOutput(OutputThread):
    """Records to segmented MOV/MP4/MXF files using PyAV with NVENC."""

    def __init__(self, cfg: OutputConfig, channel_name: str):
        super().__init__(cfg.name, channel_name, cfg.segment_seconds)
        self.cfg = cfg
        # Set by prewarm_codec() before _encoder_loop() adopts the first segment.
        self._prewarm_container: Optional[av.container.OutputContainer] = None
        self._prewarm_vstream = None
        self._prewarm_astreams: list = []
        self._prewarm_pts_offset: int = 0
        self._prewarm_start_ms: int = 0
        self._prewarm_width: int = 0
        self._prewarm_height: int = 0
        self._prewarm_framerate: tuple = (0, 0)

    def prewarm_codec(
        self,
        enc_w: int,
        enc_h: int,
        enc_fr: tuple,
        enc_pix_fmt: str,
    ) -> None:
        """Open the first segment file and prime the encoder before capture starts.

        Mirrors HlsOutput.prewarm_codec(). mpeg2video and h264_nvenc both run a
        heavy avcodec_open2() on the first encode call (~1-2 s); doing that here
        — while DeckLink is idle — prevents the encoder thread from holding the
        Python GIL long enough that the DeckLink COM callback stops firing and
        the driver's internal audio buffer overflows to its 1 s cap (which then
        crashes when the backlog is drained, see 0xC0000005 access violation).

        Caveats:
            - Two dummy black frames appear at the start of the very first
              segment. Subsequent segments roll over normally.
            - The path's timestamp is captured here, ~2 s before real frames
              arrive — close enough for archival naming.
        """
        start_ms = int(time.time() * 1000)
        path = pt.render(
            self.cfg.path_template,
            output_name=self.cfg.name,
            channel_name=self.channel_name,
            start_unix_ms=start_ms,
            seq=0,
        )
        abs_path = pt.ensure_parent(path)

        fps_num, fps_den = enc_fr if enc_fr[1] else (enc_fr[0], 1)
        rate = fractions.Fraction(fps_num, fps_den) if fps_den else fractions.Fraction(fps_num, 1)

        vcfg = self.cfg.video
        self._log.info(
            "Pre-warming codec '%s' for first segment %dx%d %s @ %d/%d → %s",
            vcfg.codec, enc_w, enc_h, vcfg.pix_fmt, fps_num, fps_den, abs_path,
        )
        try:
            container = av.open(str(abs_path), mode="w")

            codec_opts = {}
            if vcfg.profile:
                codec_opts["profile"] = vcfg.profile
            codec_opts.update(vcfg.options)

            vstream = container.add_stream(vcfg.codec, rate=rate)
            vstream.options = codec_opts
            if vcfg.bitrate:
                vstream.bit_rate = _parse_bitrate(vcfg.bitrate)
            vstream.codec_context.width = enc_w
            vstream.codec_context.height = enc_h
            vstream.codec_context.pix_fmt = vcfg.pix_fmt
            vstream.codec_context.options["preset"] = vcfg.preset

            acfg = self.cfg.audio
            if acfg.downmix == "stereo":
                out_channels = 2
            else:
                out_channels = len(acfg.channels)

            astreams: list = []
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

            # Encode two black frames to force avcodec_open2() + first packet
            # production. Do NOT flush or close — the encoder loop adopts this
            # container directly so the codec context stays alive.
            n_dummy = 2
            for pts in range(n_dummy):
                frame = av.VideoFrame(enc_w, enc_h, vcfg.pix_fmt)
                frame.pts = pts
                for pkt in vstream.encode(frame):
                    container.mux(pkt)

            self._prewarm_container = container
            self._prewarm_vstream = vstream
            self._prewarm_astreams = astreams
            self._prewarm_pts_offset = n_dummy
            self._prewarm_start_ms = start_ms
            self._prewarm_width = enc_w
            self._prewarm_height = enc_h
            self._prewarm_framerate = enc_fr
            self._log.info(
                "FileOutput codec pre-warmed (pts_offset=%d, size=%dx%d, rate=%s)",
                n_dummy, enc_w, enc_h, rate,
            )
        except Exception as exc:
            self._log.warning(
                "FileOutput codec pre-warm failed (%s) — first encode may stall",
                exc,
            )
            self._prewarm_container = None

    def _encoder_loop(self) -> None:
        # Per-segment state. Each new MOV file is its own timeline starting at
        # PTS=0; we never use hw_pts for output PTS, only for input pairing.
        container: Optional[av.container.OutputContainer] = None
        vstream = None
        astreams: list = []
        vfilter: Optional[OutputVideoFilter] = None
        afilter: Optional[OutputAudioFilter] = None

        # Setfield filter — created lazily on the first interlaced frame.
        # PyAV does not allow setting interlaced_frame/top_field_first directly,
        # so we route each frame through this tiny graph to embed the metadata.
        sf_src = None
        sf_sink = None
        sf_params: tuple = ()

        # Set after the per-output filter is built (or, if there is none, after the
        # first frame arrives). Determined by pushing test frames through the user's
        # filter chain and reading the output frame's interlaced_frame flag.
        encoder_input_interlaced: Optional[bool] = None
        encoder_input_tff: bool = True

        seg_v_pts = 0
        seg_a_pts_samples = 0
        seg_a_frames = 0
        frames_per_segment = 0

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
            self._log.info(
                "Segment opened (seq=%d start_ms=%d v_codec=%s pix_fmt=%s preset=%s "
                "a_codec=%s frames_per_segment=%d)",
                self.stats.segments_completed, start_ms,
                vcfg.codec, vcfg.pix_fmt, vcfg.preset, acfg.codec, frames_per_segment,
            )

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
                pair = self._get_pair(timeout=1.0)
                if pair is None:
                    # Timeout or stop sentinel
                    if self._stop_event.is_set():
                        self._log.info("Encoder loop exiting (stop event)")
                        break
                    continue

                frame = pair.video

                # Lazy-init per-output video filter on first frame.
                if self.cfg.video_filter and vfilter is None:
                    vfilter = OutputVideoFilter(
                        frame.width, frame.height, frame.framerate,
                        frame.fmt, self.cfg.video_filter,
                        input_interlaced=bool(frame.interlaced_frame),
                        input_top_field_first=bool(frame.top_field_first),
                    )
                    encoder_input_interlaced = vfilter.output_interlaced
                    encoder_input_tff = vfilter.output_top_field_first
                    self._log.info(
                        "Encoder input interlacing observed from filter probe: "
                        "interlaced=%s tff=%s",
                        encoder_input_interlaced, encoder_input_tff,
                    )

                # No per-output filter: encoder input = pair.video directly.
                if vfilter is None and encoder_input_interlaced is None:
                    encoder_input_interlaced = bool(frame.interlaced_frame)
                    encoder_input_tff = bool(frame.top_field_first)

                filtered_frames = list(vfilter.process(frame.data)) if vfilter else [frame.data]

                # Output filter (fps changer etc.) may drop frames; skip until output exists.
                if not filtered_frames and container is None:
                    continue

                eff_w = vfilter.output_width if vfilter else frame.width
                eff_h = vfilter.output_height if vfilter else frame.height
                eff_fmt = _map_fmt(vfilter.output_pix_fmt if vfilter else frame.fmt)
                eff_framerate = vfilter.output_framerate if vfilter else frame.framerate

                now_ms = int(time.time() * 1000)

                if container is None:
                    if self._prewarm_container is not None:
                        # Adopt the pre-warmed container from main thread. The
                        # heavy avcodec_open2() ran before DeckLink started, so
                        # the first real encode is fast and the DeckLink audio
                        # buffer doesn't overflow.
                        container = self._prewarm_container
                        vstream = self._prewarm_vstream
                        astreams = self._prewarm_astreams
                        seg_v_pts = self._prewarm_pts_offset
                        # Audio PTS continues from where pre-warm left off.
                        # pre-warm encoded only dummy video frames, so audio is at 0.
                        seg_a_pts_samples = 0
                        seg_a_frames = 0
                        fps_num, fps_den = (eff_framerate if eff_framerate[1]
                                            else (eff_framerate[0], 1))
                        rate = fractions.Fraction(fps_num, fps_den) if fps_den else fractions.Fraction(fps_num, 1)
                        frames_per_segment = round(self.cfg.segment_seconds * float(rate))
                        self._prewarm_container = None  # adopt; don't reuse
                        self._log.info(
                            "Adopted pre-warmed segment container "
                            "(pts_offset=%d, frames_per_segment=%d)",
                            seg_v_pts, frames_per_segment,
                        )
                    else:
                        open_new_segment(now_ms, eff_w, eff_h, eff_framerate)

                # Segment rollover by frame count — deterministic, exact duration.
                if seg_v_pts >= frames_per_segment:
                    close_segment()
                    open_new_segment(now_ms, eff_w, eff_h, eff_framerate)

                # If we got a format-change notification, the CaptureBuffer
                # has already flushed pre-change pairs; nothing to do here.
                if self._format_change_pending.is_set():
                    self._format_change_pending.clear()
                    self._log.info("Format change acknowledged — pairs already aligned by CaptureBuffer")

                # ── encode video ────────────────────────────────────────
                # Two paths:
                #   (A) vfilter is None AND pair.video.av_frame is available:
                #       encode the AVFrame directly. It already carries interlaced
                #       metadata from the channel-level setfield/yadif filter.
                #       No second setfield needed — avoids PyAV 17 thread-safety
                #       crash with per-frame setfield at full-HD.
                #   (B) vfilter applied a per-output transform (yadif, scale, etc.)
                #       OR av_frame is unavailable: rebuild from numpy via
                #       from_ndarray and skip setfield (output is typically
                #       progressive after a per-output filter chain anyway).
                if vfilter is None and pair.video.av_frame is not None and filtered_frames:
                    try:
                        av_frame = pair.video.av_frame
                        av_frame.pts = seg_v_pts
                        pkt_count = 0
                        _t0 = time.monotonic() if self.stats.frames_written == 0 else 0.0
                        for pkt in vstream.encode(av_frame):
                            container.mux(pkt)
                            pkt_count += 1
                        self.video_pkts_muxed += pkt_count
                        if self.stats.frames_written == 0:
                            self._log.info(
                                "First video frame encoded via AVFrame (produced %d packet(s)) in %.3fs "
                                "[interlaced=%s tff=%s]",
                                pkt_count, time.monotonic() - _t0,
                                av_frame.interlaced_frame,
                                getattr(av_frame, 'top_field_first', '?'),
                            )
                        seg_v_pts += 1
                        self.stats.frames_written += 1
                    except Exception as e:
                        self._log.error("Video encode error (AVFrame path): %s", e, exc_info=True)
                else:
                    for arr in filtered_frames:
                        try:
                            av_frame = av.VideoFrame.from_ndarray(arr, format=eff_fmt)
                            av_frame.pts = seg_v_pts
                            pkt_count = 0
                            _t0 = time.monotonic() if self.stats.frames_written == 0 else 0.0
                            for pkt in vstream.encode(av_frame):
                                container.mux(pkt)
                                pkt_count += 1
                            self.video_pkts_muxed += pkt_count
                            if self.stats.frames_written == 0:
                                self._log.info(
                                    "First video frame encoded via numpy (produced %d packet(s)) in %.3fs",
                                    pkt_count, time.monotonic() - _t0,
                                )
                            seg_v_pts += 1
                            self.stats.frames_written += 1
                        except Exception as e:
                            self._log.error("Video encode error (numpy path): %s", e, exc_info=True)

                # ── encode the audio paired with this video frame ──────
                # AVPair guarantees pair.audio.data has exactly frame_duration_samples
                # of audio (real or synthesized silence), so audio duration tracks
                # video duration sample-accurately within the segment.
                try:
                    afilter, pkt_count, samples_encoded = self._encode_audio(
                        pair, astreams, container, afilter, seg_a_pts_samples,
                    )
                    self.audio_pkts_muxed += pkt_count
                    seg_a_pts_samples += samples_encoded
                    if samples_encoded > 0:
                        seg_a_frames += 1
                    if pair.audio_is_synthesized:
                        self.stats.synthesized_audio_frames += 1
                except Exception as e:
                    self._log.error("Audio encode error: %s", e, exc_info=True)

        finally:
            self._log.info("Encoder loop finally block executing")
            if vfilter:
                vfilter.close()
            if afilter:
                afilter.close()
            self._log.info("About to close final segment")
            close_segment()
            self._log.info("Final segment closed")

    def _encode_audio(
        self,
        pair: AVPair,
        astreams: list,
        container,
        afilter: Optional[OutputAudioFilter],
        seg_a_pts_samples: int,
    ) -> tuple[Optional[OutputAudioFilter], int, int]:
        """Encode the audio attached to a single AVPair.

        Returns (possibly-initialized afilter, pkts_muxed, samples_encoded). The
        caller passes its `afilter` local in and replaces it with the returned
        value — this is how a lazy first-packet init propagates back.
        """
        apkt = pair.audio
        audio_data = _select_audio_channels(apkt.data, apkt.channels, self.cfg.audio.channels)
        if self.cfg.audio.downmix == "stereo":
            audio_data = _downmix_stereo(audio_data)

        # Lazy-init per-output audio filter on first packet.
        if self.cfg.audio_filter and afilter is None:
            afilter = OutputAudioFilter(apkt.sample_rate, audio_data.shape[1], self.cfg.audio_filter)

        chunks = list(afilter.process(audio_data)) if afilter else [audio_data]
        pkt_count = 0
        samples_encoded = 0

        for chunk in chunks:
            # int16 → int32 in upper bits: s32 codec treats full ±2^31 as
            # the audio range, so we left-shift int16 into the high bits to
            # keep amplitude correct.
            if len(astreams) == 1:
                layout = "stereo" if chunk.shape[1] == 2 else f"{chunk.shape[1]}c"
                arr = (chunk.astype(np.int32) << 16).reshape(1, -1)
                af = av.AudioFrame.from_ndarray(arr, format="s32", layout=layout)
                af.sample_rate = apkt.sample_rate
                af.pts = seg_a_pts_samples + samples_encoded
                af.time_base = fractions.Fraction(1, apkt.sample_rate)
                for pkt in astreams[0].encode(af):
                    container.mux(pkt)
                    pkt_count += 1
            else:
                # mono_per_channel — one mono frame per stream
                for ch_idx, astream in enumerate(astreams):
                    ch_data = chunk[:, ch_idx:ch_idx + 1]
                    arr = (ch_data.astype(np.int32) << 16).reshape(1, -1)
                    af = av.AudioFrame.from_ndarray(arr, format="s32", layout="mono")
                    af.sample_rate = apkt.sample_rate
                    af.pts = seg_a_pts_samples + samples_encoded
                    af.time_base = fractions.Fraction(1, apkt.sample_rate)
                    for pkt in astream.encode(af):
                        container.mux(pkt)
                        pkt_count += 1
            samples_encoded += chunk.shape[0]

            if self.audio_frames_encoded == 0:
                self._log.info(
                    "First audio frame: src_ch=%d sel_ch=%d downmix=%s "
                    "track_mode=%s streams=%d samples=%d sr=%d codec=%s synthesized=%s",
                    apkt.channels, chunk.shape[1], self.cfg.audio.downmix,
                    self.cfg.audio.track_mode, len(astreams),
                    chunk.shape[0], apkt.sample_rate, self.cfg.audio.codec,
                    pair.audio_is_synthesized,
                )
                self._log.info(
                    "First audio frame encoded (produced %d packet(s))", pkt_count,
                )
            self.audio_frames_encoded += 1

        return afilter, pkt_count, samples_encoded


# ── bitrate / pixfmt helpers ──────────────────────────────────────────────


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
