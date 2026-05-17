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
                 pix_fmt: str, spec: str):
        self._spec = spec.strip()
        self.input_width = width
        self.input_height = height
        self.input_pix_fmt = pix_fmt
        self.output_width = width
        self.output_height = height
        self.output_framerate = framerate
        self.output_pix_fmt = pix_fmt

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
            "Output video filter '%s': %dx%d %s @ %d/%d → %dx%d %s @ %d/%d",
            self._spec, width, height, pix_fmt, framerate[0], framerate[1],
            self.output_width, self.output_height, self.output_pix_fmt,
            self.output_framerate[0], self.output_framerate[1],
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
        """Push blank test frames to determine real output dimensions and framerate.

        buffersink can report framerate metadata after graph.configure() but does
        not expose output width/height until frames have been pulled. Pushing test
        frames and reading the first pulled frame's geometry is the only reliable
        way. The graph is rebuilt afterwards so real encoding starts clean.
        """
        if self._src is None or self._sink is None:
            return
        probe_count = 8
        frames_out = 0
        saved_framerate = self.output_framerate
        for i in range(probe_count):
            test = av.VideoFrame(self.input_width, self.input_height, self.input_pix_fmt)
            test.pts = i
            self._src.push(test)
            while True:
                try:
                    out = self._sink.pull()
                    if frames_out == 0:
                        self.output_width = int(out.width)
                        self.output_height = int(out.height)
                        fmt_name = getattr(out.format, "name", None) if out.format else None
                        if fmt_name:
                            self.output_pix_fmt = fmt_name
                    frames_out += 1
                except av.BlockingIOError:
                    break
        # Update framerate from probe only when _query_sink() did not already
        # determine a different rate (i.e. it is still equal to the input rate).
        if saved_framerate == input_framerate and frames_out > 0 and frames_out != probe_count:
            ratio = frames_out / probe_count
            fps_num, fps_den = input_framerate
            self.output_framerate = (round(fps_num * ratio), fps_den)
        logger.info(
            "Probed output: %d frames in → %d out | size %dx%d %s @ %d/%d",
            probe_count, frames_out,
            self.output_width, self.output_height, self.output_pix_fmt,
            self.output_framerate[0], self.output_framerate[1],
        )
        self._build_graph(self.input_width, self.input_height, input_framerate, self.input_pix_fmt)

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
