"""HLS live preview output — rolling window using PyAV's libav HLS muxer.

Consumes pre-paired AVPair objects (see capture_buffer.CaptureBuffer). The
PTS counter is just self.stats.frames_written for video and follows audio
sample-accurately because each AVPair carries exactly one frame's worth of
audio.
"""

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
from .base import AVPair, OutputThread
from .file_output import _downmix_stereo, _make_setfield_filter, _map_fmt, _parse_bitrate, _select_audio_channels
from .output_filter import OutputAudioFilter, OutputVideoFilter

logger = logging.getLogger(__name__)


class HlsOutput(OutputThread):
    """Live HLS output with a rolling window of hls_list_size .ts segments."""

    def __init__(self, cfg: OutputConfig, channel_name: str):
        super().__init__(cfg.name, channel_name, cfg.segment_seconds)
        self.cfg = cfg
        # Set by prewarm_codec() before _encoder_loop() adopts the container.
        self._prewarm_container: Optional[av.container.OutputContainer] = None
        self._prewarm_vstream = None
        self._prewarm_astream = None
        self._prewarm_pts_offset: int = 0

    def prewarm_codec(
        self,
        enc_w: int,
        enc_h: int,
        enc_fr: tuple,
        enc_pix_fmt: str,
    ) -> None:
        """Open the real HLS container and prime the video encoder before capture starts.

        NVENC creates a new session each time avcodec_open2() is called, which takes
        ~2 seconds and holds the GIL, overflowing the DeckLink ring buffer. By opening
        the container here — before DeckLink starts — and keeping it alive (never closing
        it), _encoder_loop() adopts it on the first pair and skips the ~2 s spike.

        enc_w/enc_h/enc_fr come from the pre-warmed output filter; enc_pix_fmt is
        the codec's input format (vcfg.pix_fmt, not the filter output format).
        """
        playlist_path = str(pt.ensure_parent(pt.render(
            self.cfg.path_template,
            output_name=self.cfg.name,
            channel_name=self.channel_name,
            start_unix_ms=int(time.time() * 1000),
        )))
        fps_num, fps_den = enc_fr
        rate = fractions.Fraction(fps_num, fps_den) if fps_den else fractions.Fraction(fps_num, 1)

        hls_opts = {
            "hls_time": str(self.cfg.segment_seconds),
            "hls_list_size": str(self.cfg.hls_list_size),
            "hls_flags": "delete_segments+append_list",
            "hls_segment_type": "mpegts",
        }

        vcfg = self.cfg.video
        self._log.info(
            "Pre-warming HLS codec '%s' at %dx%d %s @ %d/%d → %s",
            vcfg.codec, enc_w, enc_h, vcfg.pix_fmt, fps_num, fps_den, playlist_path,
        )
        try:
            container = av.open(playlist_path, mode="w", format="hls", options=hls_opts)

            vstream = container.add_stream(vcfg.codec, rate=rate)
            vstream.codec_context.width = enc_w
            vstream.codec_context.height = enc_h
            vstream.codec_context.pix_fmt = vcfg.pix_fmt
            if vcfg.preset:
                vstream.codec_context.options["preset"] = vcfg.preset
            if vcfg.bitrate:
                vstream.bit_rate = _parse_bitrate(vcfg.bitrate)
            if vcfg.options:
                vstream.codec_context.options.update({k: str(v) for k, v in vcfg.options.items()})
            if "g" not in vstream.codec_context.options:
                gop_frames = max(1, round(float(rate) * self.cfg.segment_seconds))
                vstream.codec_context.options["g"] = str(gop_frames)

            acfg = self.cfg.audio
            out_channels = 2 if acfg.downmix == "stereo" else len(acfg.channels)
            astream = container.add_stream(
                acfg.codec, rate=48000,
                layout="stereo" if out_channels == 2 else f"{out_channels}c",
            )
            if acfg.bitrate:
                astream.bit_rate = _parse_bitrate(acfg.bitrate)

            # Encode blank frames to call avcodec_open2() and fill the NVENC pipeline.
            # We do NOT flush or close — the session must stay alive until the encoder
            # thread adopts this container. The 2 black frames appear at the very start
            # of the live preview but are imperceptible in practice.
            n_dummy = 2
            for pts in range(n_dummy):
                frame = av.VideoFrame(enc_w, enc_h, vcfg.pix_fmt)
                frame.pts = pts
                for pkt in vstream.encode(frame):
                    container.mux(pkt)

            self._prewarm_container = container
            self._prewarm_vstream = vstream
            self._prewarm_astream = astream
            self._prewarm_pts_offset = n_dummy
            self._log.info(
                "HLS codec pre-warmed (pts_offset=%d, size=%dx%d, rate=%s)",
                n_dummy, enc_w, enc_h, rate,
            )
        except Exception as exc:
            self._log.warning(
                "HLS codec pre-warm failed (%s) — first encode will block ~2 s", exc,
            )
            self._prewarm_container = None

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
        vfilter: Optional[OutputVideoFilter] = None
        afilter: Optional[OutputAudioFilter] = None

        sf_src = None
        sf_sink = None
        sf_params: tuple = ()

        hls_opts = {
            "hls_time": str(self.cfg.segment_seconds),
            "hls_list_size": str(self.cfg.hls_list_size),
            "hls_flags": "delete_segments+append_list",
            "hls_segment_type": "mpegts",
        }

        pts_offset: int = 0   # non-zero when a pre-warmed container is adopted
        seg_a_pts_samples = 0

        # Set after the per-output filter is built (or, if there is none, after the
        # first frame arrives). Determined by pushing test frames through the user's
        # filter chain and reading the output frame's interlaced_frame flag — i.e.
        # observed, not inferred from the filter string.
        encoder_input_interlaced: Optional[bool] = None
        encoder_input_tff: bool = True

        try:
            while not self._stop_event.is_set():
                pair = self._get_pair(timeout=1.0)
                if pair is None:
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

                # fps filters drop frames; skip until we have output to encode.
                # Without this the container would be initialised before output_width/height
                # are known from a real filtered frame, locking in the wrong resolution.
                if not filtered_frames and container is None:
                    continue

                eff_w = vfilter.output_width if vfilter else frame.width
                eff_h = vfilter.output_height if vfilter else frame.height
                eff_fmt = _map_fmt(vfilter.output_pix_fmt if vfilter else frame.fmt)
                eff_framerate = vfilter.output_framerate if vfilter else frame.framerate

                # Container/stream setup on first frame — adopt the pre-warmed container
                # if prewarm_codec() ran successfully, otherwise fall back to lazy init.
                if container is None:
                    if self._prewarm_container is not None:
                        container = self._prewarm_container
                        vstream = self._prewarm_vstream
                        astream = self._prewarm_astream
                        pts_offset = self._prewarm_pts_offset
                        self._prewarm_container = None  # adopt; don't reuse
                        self._log.info(
                            "Adopted pre-warmed HLS container (pts_offset=%d)", pts_offset,
                        )
                    else:
                        fps_num, fps_den = eff_framerate if eff_framerate[1] else (eff_framerate[0], 1)
                        rate = fractions.Fraction(fps_num, fps_den) if fps_den else fractions.Fraction(fps_num, 1)
                        container = av.open(playlist_path, mode="w", format="hls", options=hls_opts)

                        vcfg = self.cfg.video
                        vstream = container.add_stream(vcfg.codec, rate=rate)
                        vstream.codec_context.width = eff_w
                        vstream.codec_context.height = eff_h
                        vstream.codec_context.pix_fmt = vcfg.pix_fmt
                        vstream.codec_context.options["preset"] = vcfg.preset
                        if vcfg.bitrate:
                            vstream.bit_rate = _parse_bitrate(vcfg.bitrate)
                        if vcfg.options:
                            vstream.codec_context.options.update({k: str(v) for k, v in vcfg.options.items()})
                        # HLS cuts at keyframe boundaries; force GOP = segment_seconds * fps.
                        if "g" not in vstream.codec_context.options:
                            gop_frames = max(1, round(float(rate) * self.cfg.segment_seconds))
                            vstream.codec_context.options["g"] = str(gop_frames)

                        acfg = self.cfg.audio
                        out_channels = 2 if acfg.downmix == "stereo" else len(acfg.channels)
                        astream = container.add_stream(acfg.codec, rate=48000, layout="stereo" if out_channels == 2 else f"{out_channels}c")
                        if acfg.bitrate:
                            astream.bit_rate = _parse_bitrate(acfg.bitrate)

                        self._log.info("HLS streams opened (size=%dx%d rate=%s codec=%s)",
                                       eff_w, eff_h, rate, vcfg.codec)

                # ── encode video ────────────────────────────────────────
                for arr in filtered_frames:
                    try:
                        av_frame = av.VideoFrame.from_ndarray(arr, format=eff_fmt)

                        # NOTE: per-frame setfield filter is permanently disabled
                        # for HLS. PyAV 17.0.1 has a thread-safety bug at full-HD
                        # where a per-frame setfield buffersink crashes when other
                        # encoder threads run concurrently. In practice HLS chains
                        # use yadif which deinterlaces, so the encoder input is
                        # already progressive (encoder_input_interlaced=False) and
                        # this block wouldn't execute anyway. If you need
                        # interlaced HLS, route via AVFrame pass-through (see
                        # FileOutput) instead of resurrecting this block.
                        if False and encoder_input_interlaced:
                            tff = encoder_input_tff
                            params = (eff_w, eff_h, eff_fmt,
                                      eff_framerate[0], eff_framerate[1], tff)
                            if params != sf_params:
                                sf_src, sf_sink = _make_setfield_filter(
                                    eff_w, eff_h, eff_fmt,
                                    eff_framerate[0], eff_framerate[1], tff,
                                )
                                sf_params = params
                                self._log.info(
                                    "setfield=%s filter ready (%dx%d %s @ %d/%d)",
                                    "tff" if tff else "bff",
                                    eff_w, eff_h, eff_fmt,
                                    eff_framerate[0], eff_framerate[1],
                                )
                            sf_src.push(av_frame)
                            try:
                                av_frame = sf_sink.pull()
                            except av.BlockingIOError:
                                self._log.warning("setfield filter produced no output")

                        av_frame.pts = pts_offset + self.stats.frames_written
                        pkt_count = 0
                        _t0 = time.monotonic() if self.stats.frames_written == 0 else 0.0
                        for pkt in vstream.encode(av_frame):
                            container.mux(pkt)
                            pkt_count += 1
                        self.video_pkts_muxed += pkt_count
                        if self.stats.frames_written == 0:
                            self._log.info("First HLS video frame encoded (produced %d packet(s)) in %.3fs",
                                           pkt_count, time.monotonic() - _t0)
                        self.stats.frames_written += 1
                    except Exception as e:
                        self._log.error("HLS video encode error: %s", e, exc_info=True)

                # ── encode the audio paired with this video frame ──────
                try:
                    afilter, pkt_count, samples_encoded = self._encode_audio(
                        pair, astream, container, afilter, seg_a_pts_samples,
                    )
                    self.audio_pkts_muxed += pkt_count
                    seg_a_pts_samples += samples_encoded
                    if pair.audio_is_synthesized:
                        self.stats.synthesized_audio_frames += 1
                except Exception as e:
                    self._log.error("HLS audio encode error: %s", e, exc_info=True)

        finally:
            self._log.info("Encoder loop finally block executing")
            if vfilter:
                vfilter.close()
            if afilter:
                afilter.close()
            if container:
                try:
                    self._log.info("Flushing HLS encoders")
                    if vstream:
                        for pkt in vstream.encode(None):
                            container.mux(pkt)
                    if astream:
                        for pkt in astream.encode(None):
                            container.mux(pkt)
                    self._log.info("Closing HLS container")
                    container.close()
                    self._log.info("HLS container closed")
                except Exception as e:
                    self._log.error("HLS flush error: %s", e)

    def _encode_audio(
        self,
        pair: AVPair,
        astream,
        container,
        afilter: Optional[OutputAudioFilter],
        seg_a_pts_samples: int,
    ) -> tuple[Optional[OutputAudioFilter], int, int]:
        """Encode the audio attached to a single AVPair (single combined stream)."""
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
            layout = "stereo" if chunk.shape[1] == 2 else f"{chunk.shape[1]}c"
            # int16 → int32 in upper bits: s32 codec treats full ±2^31 as the
            # audio range, so we left-shift int16 into the high bits to keep
            # amplitude correct.
            arr = (chunk.astype(np.int32) << 16).reshape(1, -1)
            af = av.AudioFrame.from_ndarray(arr, format="s32", layout=layout)
            af.sample_rate = apkt.sample_rate
            af.pts = seg_a_pts_samples + samples_encoded
            af.time_base = fractions.Fraction(1, apkt.sample_rate)

            if self.audio_frames_encoded == 0:
                self._log.info(
                    "First HLS audio frame: src_ch=%d sel_ch=%d downmix=%s layout=%s "
                    "samples=%d sr=%d codec=%s codec_fmt=%s synthesized=%s",
                    apkt.channels, chunk.shape[1], self.cfg.audio.downmix,
                    layout, chunk.shape[0], apkt.sample_rate,
                    self.cfg.audio.codec, astream.codec_context.format,
                    pair.audio_is_synthesized,
                )

            for pkt in astream.encode(af):
                container.mux(pkt)
                pkt_count += 1

            if self.audio_frames_encoded == 0:
                self._log.info(
                    "First HLS audio frame encoded (produced %d packet(s))", pkt_count,
                )
            samples_encoded += chunk.shape[0]
            self.audio_frames_encoded += 1

        return afilter, pkt_count, samples_encoded
