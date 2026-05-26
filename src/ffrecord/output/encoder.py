"""Unified encoder output — one class for any libav container.

Consumes pre-paired AVPair objects produced by CaptureBuffer. The container
format (mov, mp4, mxf, hls, mpegts, …) and its muxer options come entirely
from the OutputConfig; the encoder loop has no per-format branches.

Segmentation strategy:
  - `internal_splitter.enabled=True`  → app-managed: container is closed and
    reopened every N frames so each file starts at PTS=0 and is independently
    playable (archive case).
  - `internal_splitter.enabled=False` → libav-managed: container is opened once
    at the first frame and stays open; muxer-specific options (e.g. hls_time)
    drive any internal segmentation. PTS is continuous.

Per-frame encode supports two paths: AVFrame pass-through (preserves
interlaced metadata without a per-frame setfield filter, sidestepping the
PyAV 17 thread-safety bug at full-HD) and numpy→VideoFrame for paths that
strip metadata intentionally.
"""

from __future__ import annotations

import fractions
import time
from typing import Optional

import av
import numpy as np

from ..config import OutputConfig
from . import path_template as pt
from .base import AVPair, OutputThread
from .output_filter import OutputAudioFilter, OutputVideoFilter
from .util import (
    MuxCounters, build_audio_streams, build_video_stream, downmix_stereo,
    map_pix_fmt, mux_with_logging, open_container, parse_bitrate, resolve_gop,
    select_audio_channels,
)


class EncoderOutput(OutputThread):
    """One encoder loop for any libav container — config drives behaviour."""

    def __init__(self, cfg: OutputConfig, channel_name: str):
        super().__init__(cfg.name, channel_name)
        self.cfg = cfg
        # Pre-warmed state, populated by prewarm_codec(). Adopted on the first
        # real pair so avcodec_open2() doesn't stall the DeckLink COM thread.
        self._prewarm_container: Optional[av.container.OutputContainer] = None
        self._prewarm_vstream = None
        self._prewarm_astreams: list = []
        self._prewarm_pts_offset: int = 0
        self._prewarm_dims: tuple = (0, 0, (0, 0))

    # ── pre-warm ────────────────────────────────────────────────────────────

    def prewarm_codec(
        self,
        enc_w: int,
        enc_h: int,
        enc_fr: tuple,
        enc_pix_fmt: str,
    ) -> None:
        """Open the container and run a few dummy frames through the encoder so
        avcodec_open2() (which can take ~2 s for NVENC/mpeg2video) doesn't fire
        on the first real callback. The pre-warmed container is adopted by
        _encoder_loop() — never closed and reopened — so the codec session
        stays alive.
        """
        start_ms = int(time.time() * 1000)
        path = pt.render(
            self.cfg.path_template,
            output_name=self.cfg.name,
            channel_name=self.channel_name,
            start_unix_ms=start_ms,
            seq=0,
        )
        abs_path = str(pt.ensure_parent(path))

        self._log.info(
            "Pre-warming codec '%s' (%s) at %dx%d %s @ %s → %s",
            self.cfg.video.codec, self.cfg.container_format,
            enc_w, enc_h, self.cfg.video.pix_fmt, enc_fr, abs_path,
        )
        try:
            container = open_container(self.cfg, abs_path)
            vstream, _rate, _gop = build_video_stream(
                container, self.cfg.video, enc_w, enc_h, enc_fr,
            )
            astreams = build_audio_streams(container, self.cfg.audio)

            n_dummy = 2
            for ts in range(n_dummy):
                frame = av.VideoFrame(enc_w, enc_h, self.cfg.video.pix_fmt)
                frame.pts = ts
                for pkt in vstream.encode(frame):
                    container.mux(pkt)

            self._prewarm_container = container
            self._prewarm_vstream = vstream
            self._prewarm_astreams = astreams
            self._prewarm_pts_offset = n_dummy
            self._prewarm_dims = (enc_w, enc_h, enc_fr)
            self._log.info(
                "Codec pre-warmed (pts_offset=%d, size=%dx%d, rate=%s, streams=%d audio)",
                n_dummy, enc_w, enc_h, enc_fr, len(astreams),
            )
        except Exception as exc:
            self._log.warning(
                "Codec pre-warm failed (%s) — first encode may stall briefly", exc,
            )
            self._prewarm_container = None
            self._prewarm_vstream = None
            self._prewarm_astreams = []

    # ── encoder loop ────────────────────────────────────────────────────────

    def _encoder_loop(self) -> None:
        container: Optional[av.container.OutputContainer] = None
        vstream = None
        astreams: list = []
        vfilter: Optional[OutputVideoFilter] = None
        afilter: Optional[OutputAudioFilter] = None

        # Set after the per-output filter is built (or, if there is none, after
        # the first frame arrives). Currently only used in log lines — the
        # AVFrame pass-through path preserves interlace metadata automatically.
        encoder_input_interlaced: Optional[bool] = None
        encoder_input_tff: bool = True

        # Segment-local counters. With internal_splitter.enabled they reset
        # every segment; otherwise they keep counting for the lifetime of the
        # container (libav handles any sub-segmenting internally).
        seg_v_pts = 0
        seg_a_pts_samples = 0
        seg_a_frames = 0
        frames_per_segment = 0
        eff_framerate: tuple = (0, 0)

        app_managed = self.cfg.internal_splitter.enabled
        split_seconds = self.cfg.internal_splitter.seconds

        # Counters used for graduated "filter dropout" logging (L4 — wired via
        # _maybe_log_filter_dropout).
        filter_dropouts_total = 0

        def render_path() -> str:
            return str(pt.ensure_parent(pt.render(
                self.cfg.path_template,
                output_name=self.cfg.name,
                channel_name=self.channel_name,
                start_unix_ms=int(time.time() * 1000),
                seq=self.stats.segments_completed,
            )))

        def open_new_container(eff_w: int, eff_h: int, framerate: tuple) -> None:
            """Open + configure the container. Used both for the first
            container of the run AND for app-managed segment rollover.
            """
            nonlocal container, vstream, astreams
            nonlocal seg_v_pts, seg_a_pts_samples, seg_a_frames, frames_per_segment

            path = render_path()
            self._log.info(
                "Opening container: %s (size=%dx%d rate=%s format=%s)",
                path, eff_w, eff_h, framerate, self.cfg.container_format,
            )
            container = open_container(self.cfg, path)
            vstream, rate, gop_frames = build_video_stream(
                container, self.cfg.video, eff_w, eff_h, framerate,
            )
            astreams = build_audio_streams(container, self.cfg.audio)

            self._log.info(
                "Streams opened: v_codec=%s pix_fmt=%s preset=%s a_codec=%s "
                "audio_streams=%d gop=%s",
                self.cfg.video.codec, self.cfg.video.pix_fmt, self.cfg.video.preset,
                self.cfg.audio.codec, len(astreams), gop_frames,
            )

            seg_v_pts = 0
            seg_a_pts_samples = 0
            seg_a_frames = 0
            if app_managed:
                frames_per_segment = max(1, round(split_seconds * float(rate)))
                self._log.info(
                    "Segment opened (seq=%d frames_per_segment=%d)",
                    self.stats.segments_completed, frames_per_segment,
                )
            else:
                frames_per_segment = 0   # never rotates

        def close_current_container() -> None:
            """Flush + close. Used at app-managed segment boundaries AND at
            encoder-loop teardown.
            """
            nonlocal container, vstream, astreams
            if container is None:
                return
            try:
                if vstream is not None:
                    for pkt in vstream.encode(None):
                        mux_with_logging(container, pkt, self.name, self.mux_counters, kind="video")
                for s in astreams:
                    for pkt in s.encode(None):
                        mux_with_logging(container, pkt, self.name, self.mux_counters, kind="audio")
                container.close()
                if app_managed:
                    self.stats.segments_completed += 1
                self._log.info(
                    "Container closed (segments_completed=%d v_frames=%d a_frames=%d a_samples=%d)",
                    self.stats.segments_completed, seg_v_pts, seg_a_frames, seg_a_pts_samples,
                )
            except Exception as e:
                self._log.error("Error closing container: %s", e, exc_info=True)
            finally:
                container = None
                vstream = None
                astreams = []

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

                if vfilter is None and encoder_input_interlaced is None:
                    encoder_input_interlaced = bool(frame.interlaced_frame)
                    encoder_input_tff = bool(frame.top_field_first)

                frames_to_encode = list(vfilter.process(frame.data)) if vfilter else [pair.video.av_frame]

                # L4: per-output filter yielded no frames for this pair.
                # Surface it as a graduated WARNING so mid-stream filter
                # dropouts are visible instead of being silently dropped.
                if not frames_to_encode:
                    filter_dropouts_total += 1
                    self._maybe_log_filter_dropout(filter_dropouts_total)
                    if container is None:
                        continue

                eff_w = vfilter.output_width if vfilter else frame.width
                eff_h = vfilter.output_height if vfilter else frame.height
                eff_framerate = vfilter.output_framerate if vfilter else frame.framerate

                # Container/stream setup on first frame — adopt pre-warmed if
                # available, otherwise open fresh.
                if container is None:
                    if self._prewarm_container is not None:
                        container = self._prewarm_container
                        vstream = self._prewarm_vstream
                        astreams = self._prewarm_astreams
                        seg_v_pts = self._prewarm_pts_offset
                        seg_a_pts_samples = 0
                        seg_a_frames = 0
                        if app_managed:
                            fps_num, fps_den = (eff_framerate if eff_framerate[1]
                                                else (eff_framerate[0], 1))
                            rate = (fractions.Fraction(fps_num, fps_den)
                                    if fps_den else fractions.Fraction(fps_num, 1))
                            frames_per_segment = max(1, round(split_seconds * float(rate)))
                        else:
                            frames_per_segment = 0
                        self._prewarm_container = None
                        self._log.info(
                            "Adopted pre-warmed container (pts_offset=%d, app_managed=%s, "
                            "frames_per_segment=%d)",
                            seg_v_pts, app_managed, frames_per_segment,
                        )
                    else:
                        open_new_container(eff_w, eff_h, eff_framerate)

                # App-managed segment rollover. Skipped when libav handles it.
                if app_managed and seg_v_pts >= frames_per_segment:
                    close_current_container()
                    open_new_container(eff_w, eff_h, eff_framerate)

                # CaptureBuffer has already flushed pre-change pairs by the
                # time a format-change-pending flag arrives. Just acknowledge.
                if self._format_change_pending.is_set():
                    self._format_change_pending.clear()
                    self._log.info(
                        "Format change acknowledged — pairs already aligned by CaptureBuffer",
                    )

                # ── encode video ────────────────────────────────────────
                for av_frame in frames_to_encode:
                    try:
                        av_frame.pts = seg_v_pts
                        av_frame.time_base = vstream.codec_context.time_base
                        pkt_count = 0
                        _t0 = time.monotonic() if self.stats.frames_written == 0 else 0.0
                        for pkt in vstream.encode(av_frame):
                            if mux_with_logging(container, pkt, self.name, self.mux_counters, kind="video"):
                                pkt_count += 1
                        self.video_pkts_muxed += pkt_count
                        if self.stats.frames_written == 0:
                            self._log.info(
                                "First video frame encoded (produced %d packet(s)) in %.3fs "
                                "[interlaced=%s tff=%s]",
                                pkt_count, time.monotonic() - _t0,
                                getattr(av_frame, 'interlaced_frame', '?'),
                                getattr(av_frame, 'top_field_first', '?'),
                            )
                        seg_v_pts += 1
                        self.stats.frames_written += 1
                    except Exception as e:
                        self._log.error("Video encode error: %s", e, exc_info=True)

                # ── encode audio paired with this video frame ──────────
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

                # Mirror MuxCounters total into OutputStats so /status sees it
                # without a separate accessor.
                self.stats.mux_failures = self.mux_counters.total_failures

        finally:
            self._log.info("Encoder loop finally block executing")
            if vfilter:
                vfilter.close()
            if afilter:
                afilter.close()
            self._log.info("About to close final container")
            close_current_container()
            self._log.info("Final container closed")

    # ── audio encode ────────────────────────────────────────────────────────

    def _encode_audio(
        self,
        pair: AVPair,
        astreams: list,
        container,
        afilter: Optional[OutputAudioFilter],
        seg_a_pts_samples: int,
    ) -> tuple[Optional[OutputAudioFilter], int, int]:
        """Encode one AVPair's audio across the configured stream(s).

        Returns (possibly-initialised afilter, pkts_muxed, samples_encoded).
        Caller passes its `afilter` local in and replaces it with the returned
        value so a lazy first-packet init propagates back.
        """
        apkt = pair.audio
        audio_data = select_audio_channels(apkt.data, apkt.channels, self.cfg.audio.channels)
        if self.cfg.audio.downmix == "stereo":
            audio_data = downmix_stereo(audio_data)

        if self.cfg.audio_filter and afilter is None:
            afilter = OutputAudioFilter(apkt.sample_rate, audio_data.shape[1], self.cfg.audio_filter)

        chunks = list(afilter.process(audio_data)) if afilter else [audio_data]
        pkt_count = 0
        samples_encoded = 0

        for chunk in chunks:
            if len(astreams) == 1:
                layout = "stereo" if chunk.shape[1] == 2 else f"{chunk.shape[1]}c"
                arr = (chunk.astype(np.int32) << 16).reshape(1, -1)
                af = av.AudioFrame.from_ndarray(arr, format="s32", layout=layout)
                af.sample_rate = apkt.sample_rate
                af.pts = seg_a_pts_samples + samples_encoded
                af.time_base = fractions.Fraction(1, apkt.sample_rate)
                for pkt in astreams[0].encode(af):
                    if mux_with_logging(container, pkt, self.name, self.mux_counters, kind="audio"):
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
                        if mux_with_logging(container, pkt, self.name, self.mux_counters, kind="audio"):
                            pkt_count += 1
            samples_encoded += chunk.shape[0]

            if self.audio_frames_encoded == 0:
                self._log.info(
                    "First audio frame: src_ch=%d sel_ch=%d downmix=%s "
                    "track_mode=%s streams=%d samples=%d sr=%d codec=%s "
                    "synthesized=%s (produced %d packet(s))",
                    apkt.channels, chunk.shape[1], self.cfg.audio.downmix,
                    self.cfg.audio.track_mode, len(astreams),
                    chunk.shape[0], apkt.sample_rate, self.cfg.audio.codec,
                    pair.audio_is_synthesized, pkt_count,
                )
            self.audio_frames_encoded += 1

        return afilter, pkt_count, samples_encoded

    # ── L4 filter-dropout graduated logging ─────────────────────────────────

    def _maybe_log_filter_dropout(self, total: int) -> None:
        from ..sync_log import log_filter_dropout
        if total in (1, 10, 100, 1000, 10000) or (total > 10000 and total % 10000 == 0):
            log_filter_dropout(self.name, total)
