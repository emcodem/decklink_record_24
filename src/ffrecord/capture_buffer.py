"""hw_pts-based A/V pairing buffer.

Sits between the capture layer (which delivers VideoFrames and AudioPackets
from DeckLink, each carrying a 10MHz hardware timestamp) and the per-output
encoder threads (which consume complete AVPair objects via EncodingBuffer).

Why this exists
---------------
DeckLink delivers video and audio in the same callback, but they each carry
their own hw_pts. In the old design, video and audio went into separate
queues and the encoder loop drained "one video frame plus all currently
queued audio". That gave a stable but variable A/V offset per segment —
because whatever audio happened to be sitting in the queue when the first
video frame of a new MOV segment was processed determined the offset for
the entire segment. Across hours of recording, the offset shifted 80–120ms
at random segment boundaries.

This buffer eliminates that by **pairing each video frame with the exact
audio samples whose hw_pts falls inside that frame's duration window**,
before the data ever reaches the encoder queue. Output PTS values remain
zero-based per segment; only *which* audio goes with *which* video frame
changes.

Pairing rules
-------------
1. Stale audio (audio whose entire range is before the oldest buffered
   video frame's start) is discarded — it represents audio captured before
   any current video frame and has no matching video to pair with.

2. When new audio arrives whose hw_pts is >= the oldest video frame's end,
   we KNOW that frame's audio is "complete" (anything that would have
   matched has already arrived) — we can emit it now.

3. If audio for an older video frame never arrived but audio for a newer
   frame did, the older frame is emitted with synthesized silence. This
   is the "catch-up with silence" rule the user specified.

4. When the byte budget (2 GB) is exceeded, the oldest video is forcibly
   emitted with silence to keep memory bounded.

5. On format change, the buffers are flushed wholesale — pre-change pairs
   would be invalid.

All events are logged per-occurrence at appropriate levels via sync_log.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

from .output.base import AudioPacket, AVPair, VideoFrame
from .sync_log import (
    log_av_pair_audio_gap,
    log_av_pair_buffer_high,
    log_av_pair_catchup_silence,
    log_av_pair_emitted,
    log_av_pair_first,
    log_av_pair_forced_silence,
    log_av_pair_format_change_drop,
    log_av_pair_stale_audio,
)

logger = logging.getLogger("ffrecord.capture_buffer")


# 2 GB cap on buffered video bytes — see docstring for sizing rationale.
BUFFER_MAX_BYTES = 2 * 1024 * 1024 * 1024

# Issue BUFFER_HIGH warnings starting at 75% of the cap.
BUFFER_HIGH_THRESHOLD = int(BUFFER_MAX_BYTES * 0.75)

# Default jitter sample window for the status API.
JITTER_WINDOW = 1000


@dataclass
class CaptureBufferStats:
    pending_video: int = 0
    pending_audio: int = 0
    buffered_video_bytes: int = 0
    emitted_total: int = 0
    stale_audio_drops: int = 0
    audio_gaps: int = 0
    catchup_silence_frames: int = 0
    forced_silence_frames: int = 0
    last_pair_delta_ticks: int = 0
    jitter_min_ticks: int = 0
    jitter_max_ticks: int = 0


class CaptureBuffer:
    """Buffer + pair video frames with audio samples by hw_pts.

    Thread-safety: all push_* and reset() methods are safe to call from
    any thread. The pairing fan-out (the emit_callback) is invoked
    synchronously from inside push_* with the internal lock held — the
    callback should be cheap (typically EncodingBuffer.push, which is
    non-blocking).
    """

    def __init__(
        self,
        emit_callback: Callable[[AVPair], None],
        default_audio_channels: int = 8,
        default_audio_sample_rate: int = 48000,
        hw_pts_rate: int = 10_000_000,
    ):
        self._emit = emit_callback
        self._hw_pts_rate = hw_pts_rate
        self._default_audio_channels = default_audio_channels
        self._default_audio_sample_rate = default_audio_sample_rate

        self._video_buf: deque[VideoFrame] = deque()
        self._audio_buf: deque[AudioPacket] = deque()
        self._buffered_video_bytes = 0
        self._lock = threading.Lock()

        # Most recently seen real audio shape — used for silence synthesis.
        self._last_audio_channels = default_audio_channels
        self._last_audio_sample_rate = default_audio_sample_rate

        # Stats and jitter ring buffer (deltas in hw_pts ticks).
        self._stats = CaptureBufferStats()
        self._jitter: deque[int] = deque(maxlen=JITTER_WINDOW)
        self._first_pair_logged = False
        self._buffer_high_state = False

    # ── public API ──────────────────────────────────────────────────────

    def push_video(self, frame: VideoFrame) -> None:
        with self._lock:
            self._video_buf.append(frame)
            self._buffered_video_bytes += frame.data.nbytes
            self._update_buffer_high_state()
            self._try_emit_pairs()

    def push_audio(self, pkt: AudioPacket) -> None:
        with self._lock:
            # Track most recent real audio shape for silence templates.
            self._last_audio_channels = pkt.channels
            self._last_audio_sample_rate = pkt.sample_rate

            # If the entire packet is older than the oldest pending video,
            # it can never be paired — discard now.
            if self._video_buf and pkt.hw_pts_valid:
                oldest_video_start = self._video_buf[0].hw_pts
                pkt_end_ticks = pkt.hw_pts + self._audio_duration_ticks(pkt)
                if pkt_end_ticks <= oldest_video_start:
                    self._stats.stale_audio_drops += 1
                    log_av_pair_stale_audio(
                        pkt.hw_pts, pkt.data.shape[0], oldest_video_start,
                    )
                    return

            self._audio_buf.append(pkt)
            self._try_emit_pairs()

    def reset(self) -> None:
        """Drop all buffered frames and packets. Called on format change."""
        with self._lock:
            pv = len(self._video_buf)
            pa = len(self._audio_buf)
            self._video_buf.clear()
            self._audio_buf.clear()
            self._buffered_video_bytes = 0
            self._buffer_high_state = False
            if pv or pa:
                log_av_pair_format_change_drop(pv, pa)

    def get_stats(self) -> CaptureBufferStats:
        """Returns a snapshot of the current buffer state and counters."""
        with self._lock:
            self._stats.pending_video = len(self._video_buf)
            self._stats.pending_audio = len(self._audio_buf)
            self._stats.buffered_video_bytes = self._buffered_video_bytes
            if self._jitter:
                self._stats.jitter_min_ticks = min(self._jitter)
                self._stats.jitter_max_ticks = max(self._jitter)
            return self._stats

    # ── pairing core (called under _lock) ───────────────────────────────

    def _try_emit_pairs(self) -> None:
        """Emit as many AVPairs as we can given the current buffer state."""
        while self._video_buf:
            V = self._video_buf[0]

            # Decide whether V's audio is "complete enough" to emit now.
            # See docstring rules 2, 3, 4.
            decision = self._can_decide_for(V)
            if decision == "wait":
                break

            self._video_buf.popleft()
            self._buffered_video_bytes -= V.data.nbytes

            if decision == "force_silence":
                # Buffer cap reached and audio still hasn't arrived for V.
                self._stats.forced_silence_frames += 1
                log_av_pair_forced_silence(
                    V.hw_pts, self._buffered_video_bytes, len(self._video_buf),
                )
                self._emit_pair(V, audio_data=None, synthesized=True)
                continue

            if decision == "catchup_silence":
                # Audio arrived for a NEWER video frame, skipping over V.
                # V's audio is presumed lost — emit V with silence.
                self._stats.catchup_silence_frames += 1
                next_audio_hw_pts = self._audio_buf[0].hw_pts if self._audio_buf else 0
                log_av_pair_catchup_silence(
                    V.hw_pts, next_audio_hw_pts, frames_caught_up=1,
                )
                self._emit_pair(V, audio_data=None, synthesized=True)
                continue

            # decision == "extract": pull V's audio out of the audio buffer.
            v_end = V.hw_pts + self._frame_duration_ticks(V)
            audio_data, gap_samples = self._extract_audio_range(V.hw_pts, v_end)

            if audio_data is None or gap_samples > 0:
                # Gap detected (partial or missing audio for V).
                self._stats.audio_gaps += 1
                expected = self._expected_samples(V)
                got = audio_data.shape[0] if audio_data is not None else 0
                log_av_pair_audio_gap(V.hw_pts, expected, got)
                self._emit_pair(V, audio_data=None, synthesized=True)
            else:
                self._emit_pair(V, audio_data=audio_data, synthesized=False)

        self._update_buffer_high_state()

    def _can_decide_for(self, V: VideoFrame) -> str:
        """Return one of: 'wait', 'extract', 'catchup_silence', 'force_silence'."""
        v_end = V.hw_pts + self._frame_duration_ticks(V)

        # Bypass hw_pts logic entirely when timestamps are unreliable.
        if not V.hw_pts_valid:
            return "extract"

        if self._buffered_video_bytes > BUFFER_MAX_BYTES:
            # Memory cap exceeded — must drain regardless of audio state.
            if self._audio_buf:
                # Try to extract whatever overlaps; gap detection will handle partials.
                return "extract"
            return "force_silence"

        if not self._audio_buf:
            return "wait"

        # We have audio buffered. The frontmost packet tells us where audio
        # currently picks up.
        first_audio = self._audio_buf[0]
        if not first_audio.hw_pts_valid:
            return "extract"

        if first_audio.hw_pts >= v_end:
            # Audio buffer's earliest packet is already past V's end. V's
            # audio must be lost (audio arrives in order from DeckLink).
            return "catchup_silence"

        # Audio overlaps V. Now check whether enough audio has arrived to
        # cover V's full range — we need at least one packet starting at or
        # after v_end (proving that V's window is bounded by available data).
        last_audio = self._audio_buf[-1]
        last_end = last_audio.hw_pts + self._audio_duration_ticks(last_audio)
        if last_end >= v_end:
            return "extract"

        return "wait"

    def _extract_audio_range(
        self, start_ticks: int, end_ticks: int,
    ) -> tuple[Optional[np.ndarray], int]:
        """Slice samples from _audio_buf covering [start_ticks, end_ticks).

        Returns (concatenated_samples_or_None, missing_samples_count).
        - If extraction is complete and contiguous: (np.ndarray, 0)
        - If there's a gap or audio ran out: (partial_or_None, missing>0)
        """
        sample_rate = self._last_audio_sample_rate
        expected_samples = (end_ticks - start_ticks) * sample_rate // self._hw_pts_rate

        chunks: list[np.ndarray] = []
        cursor = start_ticks

        while self._audio_buf and cursor < end_ticks:
            pkt = self._audio_buf[0]
            pkt_end = pkt.hw_pts + self._audio_duration_ticks(pkt)

            # Skip / discard packets entirely before cursor.
            if pkt_end <= cursor:
                self._audio_buf.popleft()
                continue

            # If next packet starts after cursor, there's a gap.
            if pkt.hw_pts > cursor:
                # Gap from cursor to pkt.hw_pts. Treat as missing.
                break

            # Slice the overlap [cursor, min(pkt_end, end_ticks)).
            overlap_end = min(pkt_end, end_ticks)
            start_idx = (cursor - pkt.hw_pts) * pkt.sample_rate // self._hw_pts_rate
            cursor = overlap_end

            if pkt_end <= end_ticks:
                # Packet fully consumed — take all samples from start_idx rather
                # than re-deriving end_idx from ticks. Re-deriving loses 1 sample
                # when the packet is a split remainder whose length doesn't
                # round-trip cleanly (e.g. 682 * 10M//48k * 48k//10M = 681).
                chunks.append(pkt.data[start_idx:])
                self._audio_buf.popleft()
            else:
                # Remainder belongs to the next video frame — leave a
                # trimmed copy in the buffer so the next call sees it.
                end_idx = (overlap_end - pkt.hw_pts) * pkt.sample_rate // self._hw_pts_rate
                chunks.append(pkt.data[start_idx:end_idx])
                self._audio_buf[0] = AudioPacket(
                    data=pkt.data[end_idx:],
                    sample_rate=pkt.sample_rate,
                    channels=pkt.channels,
                    hw_pts=overlap_end,
                    hw_pts_rate=pkt.hw_pts_rate,
                    hw_pts_valid=pkt.hw_pts_valid,
                )
                break

        got_samples = sum(c.shape[0] for c in chunks)
        missing = max(0, expected_samples - got_samples)

        if not chunks:
            return None, expected_samples
        if missing > 0:
            return np.concatenate(chunks), missing
        return np.concatenate(chunks), 0

    def _emit_pair(
        self,
        V: VideoFrame,
        audio_data: Optional[np.ndarray],
        synthesized: bool,
    ) -> None:
        """Build the AVPair and fire emit_callback. Called with _lock held."""
        sample_rate = self._last_audio_sample_rate
        channels = self._last_audio_channels
        expected_samples = self._expected_samples(V)

        if audio_data is None or synthesized:
            # Synthesize silence sized to the frame.
            audio_data = np.zeros((expected_samples, channels), dtype=np.int16)
            audio_hw_pts = V.hw_pts
        else:
            audio_hw_pts = V.hw_pts
            # If real audio extraction came up short of expected_samples,
            # pad with silence so output audio duration always == video duration.
            if audio_data.shape[0] < expected_samples:
                pad = np.zeros(
                    (expected_samples - audio_data.shape[0], audio_data.shape[1]),
                    dtype=audio_data.dtype,
                )
                audio_data = np.concatenate([audio_data, pad], axis=0)

        audio_pkt = AudioPacket(
            data=audio_data,
            sample_rate=sample_rate,
            channels=channels,
            hw_pts=audio_hw_pts,
            hw_pts_rate=self._hw_pts_rate,
            hw_pts_valid=V.hw_pts_valid,
        )

        pair = AVPair(video=V, audio=audio_pkt, audio_is_synthesized=synthesized)

        # Stats
        self._stats.emitted_total += 1
        delta = audio_hw_pts - V.hw_pts
        self._stats.last_pair_delta_ticks = delta
        self._jitter.append(delta)

        if not self._first_pair_logged:
            self._first_pair_logged = True
            log_av_pair_first(V.hw_pts, audio_hw_pts, self._hw_pts_rate)

        log_av_pair_emitted(
            V.hw_pts, audio_hw_pts, audio_data.shape[0],
            pending_video=len(self._video_buf), pending_audio=len(self._audio_buf),
            synthesized=synthesized,
        )

        self._emit(pair)

    # ── helpers ─────────────────────────────────────────────────────────

    def _frame_duration_ticks(self, V: VideoFrame) -> int:
        fps_num, fps_den = V.framerate
        if fps_num <= 0 or fps_den <= 0:
            # Should never happen — VideoFrame.framerate comes from the
            # filter graph's configured output. If we hit this, something
            # upstream is producing nonsense; pairing will misbehave.
            logger.error(
                "Invalid VideoFrame.framerate %s — falling back to 50fps "
                "(1/50s frame duration). Pairing will likely be wrong.",
                V.framerate,
            )
            return self._hw_pts_rate // 50
        return self._hw_pts_rate * fps_den // fps_num

    def _audio_duration_ticks(self, pkt: AudioPacket) -> int:
        samples = pkt.data.shape[0]
        sr = max(1, pkt.sample_rate)
        return samples * self._hw_pts_rate // sr

    def _expected_samples(self, V: VideoFrame) -> int:
        sr = self._last_audio_sample_rate
        return self._frame_duration_ticks(V) * sr // self._hw_pts_rate

    def _update_buffer_high_state(self) -> None:
        """Edge-triggered BUFFER_HIGH warnings."""
        high = self._buffered_video_bytes >= BUFFER_HIGH_THRESHOLD
        if high and not self._buffer_high_state:
            self._buffer_high_state = True
            log_av_pair_buffer_high(
                len(self._video_buf), len(self._audio_buf),
                self._buffered_video_bytes, BUFFER_HIGH_THRESHOLD,
            )
        elif not high:
            self._buffer_high_state = False
