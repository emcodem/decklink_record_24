"""Per-output video and audio filtergraphs applied inside the encoder loop.

Sits downstream of the channel-level InputVideoFilter in capture/input_filter.py.
Input frames are already decoded numpy arrays (e.g. yuv420p); these filters
apply an additional ffmpeg-style chain before the encoder sees the frame.

Typical uses:
    video_filter: "scale=640:-2"          # downscale HLS preview
    audio_filter: "volume=0.5"            # attenuate one output
"""

from __future__ import annotations

import logging
from typing import Generator, Optional

import av
import numpy as np

from ..capture.input_filter import _parse_filter_chain

logger = logging.getLogger("ffrecord.output.filter")


def _av_layout(channels: int) -> str:
    if channels == 1:
        return "mono"
    if channels == 2:
        return "stereo"
    return f"{channels}c"


class OutputVideoFilter:
    """Apply an ffmpeg-style video filter chain to pre-decoded numpy frames.

    Input pix_fmt must match the format of the numpy arrays being pushed.
    Output properties (width, height, pix_fmt) are queried from the sink after
    graph.configure() and updated from the first pulled frame.
    """

    def __init__(self, width: int, height: int, framerate: tuple[int, int],
                 pix_fmt: str, spec: str,
                 input_interlaced: bool = False, input_top_field_first: bool = True):
        self._spec = spec.strip()
        self.input_width = width
        self.input_height = height
        self.input_pix_fmt = pix_fmt
        self.input_interlaced = input_interlaced
        self.input_top_field_first = input_top_field_first
        self.output_width = width
        self.output_height = height
        self.output_framerate = framerate
        self.output_pix_fmt = pix_fmt
        # Observed from probe — see _probe_output().
        self.output_interlaced = input_interlaced
        self.output_top_field_first = input_top_field_first

        self._graph: Optional[av.filter.Graph] = None
        self._src: Optional[av.filter.FilterContext] = None
        self._sink: Optional[av.filter.FilterContext] = None
        self._output_learned = False
        self._push_count = 0

        self._build_graph(width, height, framerate, pix_fmt)
        self._query_sink()
        # Always probe with test frames: buffersink exposes framerate metadata but
        # not output dimensions before real frames are pushed. Without this, output_width
        # stays at input width (1920) when a scale filter is in the chain, causing the
        # encoder to be initialised at the wrong resolution.
        self._probe_output(framerate)
        logger.info(
            "Output video filter '%s': %dx%d %s @ %d/%d (interlaced=%s tff=%s) "
            "→ %dx%d %s @ %d/%d (interlaced=%s tff=%s)",
            self._spec, width, height, pix_fmt, framerate[0], framerate[1],
            self.input_interlaced, self.input_top_field_first,
            self.output_width, self.output_height, self.output_pix_fmt,
            self.output_framerate[0], self.output_framerate[1],
            self.output_interlaced, self.output_top_field_first,
        )

    def _build_graph(self, width: int, height: int,
                     framerate: tuple[int, int], pix_fmt: str) -> None:
        graph = av.filter.Graph()
        fps_num, fps_den = framerate
        pix_fmt_av = av.video.format.VideoFormat(pix_fmt).name
        src = graph.add("buffer",
            f"video_size={width}x{height}:"
            f"pix_fmt={pix_fmt_av}:"
            f"time_base={fps_den}/{fps_num}:"
            f"frame_rate={fps_num}/{fps_den}:"
            f"pixel_aspect=1/1"
        )
        sink = graph.add("buffersink")

        chain = _parse_filter_chain(self._spec)
        if not chain or chain[-1][0] != "format":
            chain.append(("format", pix_fmt))

        prev = src
        for name, args in chain:
            node = graph.add(name, args) if args else graph.add(name)
            prev.link_to(node)
            prev = node
        prev.link_to(sink)
        graph.configure()

        self._graph, self._src, self._sink = graph, src, sink

    def _query_sink(self) -> None:
        sink = self._sink
        if sink is None:
            return
        try:
            w = sink.width
            if w:
                self.output_width = int(w)
        except (AttributeError, TypeError):
            pass
        try:
            h = sink.height
            if h:
                self.output_height = int(h)
        except (AttributeError, TypeError):
            pass
        try:
            fmt = sink.format
            if fmt is not None:
                name = getattr(fmt, "name", None) or str(fmt)
                if name:
                    self.output_pix_fmt = name
        except (AttributeError, TypeError):
            self.output_pix_fmt = self.input_pix_fmt
        try:
            fr = sink.frame_rate
            if fr is not None and getattr(fr, "denominator", 0) and getattr(fr, "numerator", 0):
                self.output_framerate = (int(fr.numerator), int(fr.denominator))
        except (AttributeError, TypeError):
            pass

    def _probe_output(self, input_framerate: tuple[int, int]) -> None:
        """Push test frames to learn output geometry, framerate, AND interlacing.

        buffersink reports framerate metadata after graph.configure() but exposes
        no geometry until frames have been pulled. For interlacing we go one step
        further: rebuild a probe-only graph that injects setfield at the front so
        the user's filter chain sees test frames marked exactly like real capture
        will be. Whatever the chain produces — passthrough, deinterlace, or its
        own setfield — we read back the interlaced_frame/top_field_first of the
        pulled frame and store it. The encoder loop uses those observed flags to
        decide whether to apply its own setfield before encoding.
        """
        if self._src is None or self._sink is None:
            return
        probe_graph, probe_src, probe_sink = self._build_probe_graph(
            self.input_width, self.input_height, input_framerate, self.input_pix_fmt,
            self.input_interlaced, self.input_top_field_first,
        )
        probe_count = 8
        frames_out = 0
        observed_interlaced: Optional[bool] = None
        observed_tff: Optional[bool] = None
        observed_time_base = None
        for i in range(probe_count):
            test = av.VideoFrame(self.input_width, self.input_height, self.input_pix_fmt)
            test.pts = i
            probe_src.push(test)
            while True:
                try:
                    out = probe_sink.pull()
                    if frames_out == 0:
                        self.output_width = int(out.width)
                        self.output_height = int(out.height)
                        fmt_name = getattr(out.format, "name", None) if out.format else None
                        if fmt_name:
                            self.output_pix_fmt = fmt_name
                        observed_interlaced = bool(getattr(out, "interlaced_frame", False))
                        observed_tff = bool(getattr(out, "top_field_first", True))
                        observed_time_base = getattr(out, "time_base", None)
                    frames_out += 1
                except av.BlockingIOError:
                    break
        # Each output frame's time_base is the filter chain's output frame
        # duration — libav sets it to 1/output_fps for every chain we care
        # about (passthrough, scale, yadif, fps, and any combination). So
        # frame_rate = 1 / time_base, i.e. (denominator, numerator).
        # This is the authoritative signal: it ignores startup transients
        # (yadif buffers 1 frame, so an 8-frame probe ratio would round to
        # 22 fps for a chain that definitively outputs 25) and works without
        # parsing the filter spec text. PyAV 17's buffersink doesn't expose
        # frame_rate / time_base attributes on the FilterContext itself, so
        # reading them off a pulled frame is the supported path.
        if (observed_time_base is not None
                and getattr(observed_time_base, "numerator", 0)
                and getattr(observed_time_base, "denominator", 0)):
            self.output_framerate = (
                int(observed_time_base.denominator),
                int(observed_time_base.numerator),
            )
        if observed_interlaced is not None:
            self.output_interlaced = observed_interlaced
            self.output_top_field_first = observed_tff if observed_tff is not None else True
        logger.info(
            "Probed output: %d frames in → %d out | size %dx%d %s @ %d/%d | "
            "input(interlaced=%s tff=%s) → output(interlaced=%s tff=%s)",
            probe_count, frames_out,
            self.output_width, self.output_height, self.output_pix_fmt,
            self.output_framerate[0], self.output_framerate[1],
            self.input_interlaced, self.input_top_field_first,
            self.output_interlaced, self.output_top_field_first,
        )
        self._build_graph(self.input_width, self.input_height, input_framerate, self.input_pix_fmt)

    def _build_probe_graph(
        self, width: int, height: int, framerate: tuple[int, int],
        pix_fmt: str, interlaced: bool, top_field_first: bool,
    ):
        """Build a probe-only graph that pre-marks frames with the real input's
        interlace flags before they enter the user's filter chain.

        Test frames created by av.VideoFrame(...) are progressive with no
        interlaced_frame/top_field_first set. Without setfield at the front,
        yadif sees progressive input and is a no-op; a passthrough chain
        would also report progressive output. Neither reflects what happens
        with real DeckLink frames. Injecting setfield reproduces the real
        pipeline exactly during probing.
        """
        graph = av.filter.Graph()
        fps_num, fps_den = framerate
        pix_fmt_av = av.video.format.VideoFormat(pix_fmt).name
        src = graph.add("buffer",
            f"video_size={width}x{height}:"
            f"pix_fmt={pix_fmt_av}:"
            f"time_base={fps_den}/{fps_num}:"
            f"frame_rate={fps_num}/{fps_den}:"
            f"pixel_aspect=1/1"
        )
        sink = graph.add("buffersink")

        chain = _parse_filter_chain(self._spec)
        if not chain or chain[-1][0] != "format":
            chain.append(("format", pix_fmt))
        if interlaced:
            chain.insert(0, ("setfield", "tff" if top_field_first else "bff"))

        prev = src
        for name, args in chain:
            node = graph.add(name, args) if args else graph.add(name)
            prev.link_to(node)
            prev = node
        prev.link_to(sink)
        graph.configure()
        return graph, src, sink

    def process(self, arr: np.ndarray) -> Generator[np.ndarray, None, None]:
        av_frame = av.VideoFrame.from_ndarray(arr, format=self.input_pix_fmt)
        av_frame.pts = self._push_count
        self._push_count += 1
        self._src.push(av_frame)
        while True:
            try:
                out = self._sink.pull()
            except av.BlockingIOError:
                break
            if not self._output_learned:
                self._output_learned = True
                try:
                    self.output_width = int(out.width)
                    self.output_height = int(out.height)
                    fmt_name = getattr(out.format, "name", None) if out.format else None
                    if fmt_name:
                        self.output_pix_fmt = fmt_name
                except (AttributeError, TypeError):
                    pass
            yield out.to_ndarray(format=self.output_pix_fmt)

    def close(self) -> None:
        self._graph = self._src = self._sink = None


class OutputAudioFilter:
    """Apply an ffmpeg-style audio filter chain to (samples, channels) int16 arrays.

    The filter is applied after channel selection and downmix, so channels here
    is the post-selection channel count. The chain must not change sample format
    or channel count (an aformat filter enforcing s16 is appended automatically).
    """

    def __init__(self, sample_rate: int, channels: int, spec: str):
        self._spec = spec.strip()
        self.sample_rate = sample_rate
        self.channels = channels

        self._graph: Optional[av.filter.Graph] = None
        self._src: Optional[av.filter.FilterContext] = None
        self._sink: Optional[av.filter.FilterContext] = None

        self._build_graph(sample_rate, channels)
        logger.info("Output audio filter '%s': %dch @ %dHz", self._spec, channels, sample_rate)

    def _build_graph(self, sample_rate: int, channels: int) -> None:
        graph = av.filter.Graph()
        layout = _av_layout(channels)
        src = graph.add("abuffer",
            f"sample_rate={sample_rate}:sample_fmt=s16:"
            f"channel_layout={layout}:time_base=1/{sample_rate}"
        )
        sink = graph.add("abuffersink")

        chain = _parse_filter_chain(self._spec)
        # Enforce s16 output so downstream encoding sees a predictable format.
        if not chain or chain[-1][0] != "aformat":
            chain.append(("aformat", f"sample_fmts=s16:channel_layouts={layout}"))

        prev = src
        for name, args in chain:
            node = graph.add(name, args) if args else graph.add(name)
            prev.link_to(node)
            prev = node
        prev.link_to(sink)
        graph.configure()

        self._graph, self._src, self._sink = graph, src, sink

    def process(self, arr: np.ndarray) -> Generator[np.ndarray, None, None]:
        """Feed (samples, channels) int16; yield filtered (samples, channels) int16 chunks."""
        layout = _av_layout(arr.shape[1])
        af = av.AudioFrame.from_ndarray(arr.reshape(1, -1), format="s16", layout=layout)
        af.sample_rate = self.sample_rate
        self._src.push(af)
        while True:
            try:
                out = self._sink.pull()
            except av.BlockingIOError:
                break
            yield out.to_ndarray().reshape(-1, arr.shape[1]).astype(np.int16)

    def close(self) -> None:
        self._graph = self._src = self._sink = None
