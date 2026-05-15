"""Video filter graph driven by an ffmpeg-style filter chain.

When `spec` is empty, this is a passthrough — no filter graph is built; raw
uyvy422 frames are yielded unchanged. When `spec` is non-empty, a PyAV filter
graph is built from the comma-separated chain, configured, and its actual
output properties (width, height, framerate, pix_fmt) are queried so the
encoder downstream can be initialized with the real numbers — e.g. a
`bwdif=mode=1` that doubles 25p → 50p is reflected in `output_framerate`.

Filter chain syntax: `name=args[,name=args...]`. The splitter respects
ffmpeg's quoting rules — single-quoted regions (`text='hello, world'`) and
backslash escapes (`text=hello\, world`) protect commas from the top-level
split, so most working ffmpeg `-vf` strings can be pasted verbatim.

Usage:
    f = VideoFilter(1920, 1080, (25000, 1000),
                    spec='yadif=mode=0:parity=auto:deint=interlaced,format=yuv420p',
                    pix_fmt='yuv420p')
    print(f.output_framerate, f.output_pix_fmt, f.output_width, f.output_height)
    for arr in f.process(frame_bytes, w, h, row_bytes):
        consume(arr)
"""

from __future__ import annotations

import logging
from typing import Generator, Optional

import av
import numpy as np

logger = logging.getLogger("ffrecord.capture.filter")


def _split_top_level_commas(spec: str) -> list[str]:
    """Split on commas that are outside single-quoted regions and not backslash-escaped.

    Mirrors ffmpeg's top-level filter-chain splitting so a working `-vf` string
    can be pasted verbatim. The quote/escape characters are preserved in the
    output so libavfilter's per-filter arg parser sees them.
    """
    parts: list[str] = []
    buf: list[str] = []
    in_quote = False
    i = 0
    while i < len(spec):
        c = spec[i]
        if c == "\\" and i + 1 < len(spec):
            buf.append(c)
            buf.append(spec[i + 1])
            i += 2
            continue
        if c == "'":
            in_quote = not in_quote
            buf.append(c)
            i += 1
            continue
        if c == "," and not in_quote:
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    parts.append("".join(buf))
    return parts


def _parse_filter_chain(spec: str) -> list[tuple[str, str]]:
    """Parse a ffmpeg-style filter chain into [(name, args), ...].

    Handles quoted regions (`'…'`) and backslash escapes (`\\,`) inside args,
    so strings like `drawtext=text='hello, world':fontsize=20,format=yuv420p`
    parse correctly.
    """
    out: list[tuple[str, str]] = []
    for part in _split_top_level_commas(spec):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            name, args = part.split("=", 1)
            out.append((name.strip(), args.strip()))
        else:
            out.append((part, ""))
    return out


class VideoFilter:
    """ffmpeg-style filter graph + passthrough mode. Single instance per channel.

    Output properties are inspected from the buffersink after `graph.configure()`,
    falling back to source properties when the buffersink doesn't expose them.
    """

    def __init__(self, width: int, height: int, framerate: tuple[int, int],
                 spec: str = "", pix_fmt: str = "yuv420p"):
        self.input_width = width
        self.input_height = height
        self.input_framerate = framerate
        self.requested_pix_fmt = pix_fmt
        self._spec = spec.strip()

        # Output properties — start as a copy of input; updated after graph build.
        self.output_width = width
        self.output_height = height
        self.output_framerate: tuple[int, int] = framerate
        self.output_pix_fmt = "uyvy422"

        self._graph: Optional[av.filter.Graph] = None
        self._src: Optional[av.filter.FilterContext] = None
        self._sink: Optional[av.filter.FilterContext] = None
        # Track whether we've adopted the actual output spec from a real pulled frame.
        self._output_learned_from_frame = False

        if self._spec:
            self._build_graph()
            self._query_sink()
            logger.info(
                "Filter graph configured: '%s' | input %dx%d uyvy422 @ %d/%d → "
                "output %dx%d %s @ %d/%d",
                self._spec,
                self.input_width, self.input_height,
                self.input_framerate[0], self.input_framerate[1],
                self.output_width, self.output_height, self.output_pix_fmt,
                self.output_framerate[0], self.output_framerate[1],
            )
        else:
            logger.info(
                "No video_filter configured — passthrough %dx%d uyvy422 @ %d/%d",
                self.input_width, self.input_height,
                self.input_framerate[0], self.input_framerate[1],
            )

    def _build_graph(self) -> None:
        graph = av.filter.Graph()
        fps_num, fps_den = self.input_framerate
        buffer_args = (
            f"video_size={self.input_width}x{self.input_height}:"
            f"pix_fmt={av.video.format.VideoFormat('uyvy422').name}:"
            f"time_base={fps_den}/{fps_num}:"
            f"pixel_aspect=1/1"
        )
        src = graph.add("buffer", buffer_args)
        sink = graph.add("buffersink")

        chain = _parse_filter_chain(self._spec)
        # Ensure the chain ends with a format filter matching requested_pix_fmt so
        # to_ndarray() gets predictable pixel layout and the encoder receives the
        # configured target format. Skip if the user explicitly ended with format=...
        if not chain or chain[-1][0] != "format":
            chain.append(("format", self.requested_pix_fmt))

        prev = src
        for name, args in chain:
            node = graph.add(name, args) if args else graph.add(name)
            prev.link_to(node)
            prev = node
        prev.link_to(sink)
        graph.configure()

        self._graph = graph
        self._src = src
        self._sink = sink

    def _query_sink(self) -> None:
        """Read actual output properties from the buffersink.

        PyAV's BufferSink exposes width/height/format/frame_rate after configure().
        We use try/except per attribute because the property surface has shifted
        across PyAV versions.
        """
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
            # Fall back to the requested pix_fmt — the auto-appended `format`
            # filter should make it correct.
            self.output_pix_fmt = self.requested_pix_fmt

        try:
            fr = sink.frame_rate
            if fr is not None and getattr(fr, "denominator", 0):
                self.output_framerate = (int(fr.numerator), int(fr.denominator))
        except (AttributeError, TypeError):
            pass

    def process(self, frame_bytes: bytes, width: int, height: int,
                row_bytes: int) -> Generator[np.ndarray, None, None]:
        """Feed one captured uyvy422 frame; yield 0 or more output frames."""
        if self._graph is None:
            # Passthrough: yield the raw UYVY bytes repacked to numpy.
            stride = row_bytes if row_bytes else width * 2
            arr = np.frombuffer(frame_bytes, dtype=np.uint8)
            if arr.size == height * stride:
                arr = arr.reshape((height, stride))[:, :width * 2]
            yield arr
            return

        av_frame = av.VideoFrame(width, height, "uyvy422")
        plane = av_frame.planes[0]
        input_stride = row_bytes if row_bytes else width * 2
        if input_stride == plane.line_size and len(frame_bytes) == plane.buffer_size:
            plane.update(frame_bytes)
        else:
            src_arr = np.frombuffer(frame_bytes, dtype=np.uint8).reshape((height, input_stride))
            padded = np.zeros((height, plane.line_size), dtype=np.uint8)
            padded[:, :width * 2] = src_arr[:, :width * 2]
            plane.update(padded.tobytes())

        self._src.push(av_frame)
        while True:
            try:
                out = self._sink.pull()
            except av.BlockingIOError:
                break
            # Adopt authoritative output spec from the first real pulled frame —
            # width/height/format are always reliable on a pulled frame even when
            # the buffersink properties aren't exposed.
            if not self._output_learned_from_frame:
                self._output_learned_from_frame = True
                try:
                    self.output_width = int(out.width)
                    self.output_height = int(out.height)
                except (AttributeError, TypeError):
                    pass
                try:
                    fmt_name = getattr(out.format, "name", None) if out.format else None
                    if fmt_name:
                        self.output_pix_fmt = fmt_name
                except (AttributeError, TypeError):
                    pass
                logger.info(
                    "Filter output adopted from first pulled frame: %dx%d %s "
                    "(framerate %d/%d as queried from sink)",
                    self.output_width, self.output_height, self.output_pix_fmt,
                    self.output_framerate[0], self.output_framerate[1],
                )
            yield out.to_ndarray(format=self.output_pix_fmt)

    def close(self) -> None:
        self._graph = None
        self._src = None
        self._sink = None


# Backwards-compatible alias so existing imports keep working during the rename.
Deinterlacer = VideoFilter
