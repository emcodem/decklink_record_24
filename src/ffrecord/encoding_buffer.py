"""Per-output bounded queue between the pairing layer and an encoder thread.

Each OutputThread (FileOutput, HlsOutput) owns one EncodingBuffer. The
capture/pairing side calls push() with a complete AVPair; the encoder side
calls get() with a timeout. When the queue is full, push() drops the
incoming item and counts the drop — same backpressure policy as the
previous separate _video_queue, just consolidated into one place.

This class is intentionally generic (no AVPair import) to keep it free of
circular dependencies with output/base.py, where AVPair lives.
"""

from __future__ import annotations

import queue
import threading
from typing import Generic, Optional, TypeVar

from .sync_log import (
    log_encoding_buffer_drop,
    log_encoding_buffer_high,
    log_encoding_buffer_recovered,
)

T = TypeVar("T")


class EncodingBuffer(Generic[T]):
    """Bounded FIFO queue with drop counting and high-water-mark warnings.

    Sizing is set per OutputThread once the framerate is known. With a
    target of 10 seconds at 50 fps, capacity is 500 items. The queue can
    accept None as a stop sentinel (see stop_sentinel()).

    High-water threshold: when qsize crosses (capacity - margin), a HIGH
    warning fires; when it falls back below, a RECOVERED info is logged.
    The margin defaults to 10% of capacity (min 2).
    """

    def __init__(self, name: str, capacity: int, high_margin_pct: float = 0.10):
        self.name = name
        self.capacity = capacity
        self._high_margin = max(2, int(capacity * high_margin_pct))
        self._q: queue.Queue[Optional[T]] = queue.Queue(maxsize=capacity)
        self._lock = threading.Lock()

        # stats
        self.dropped_total = 0
        self.qsize_peak = 0
        self._high_state = False   # True while qsize >= capacity - margin

    # ── producer side (called from capture/pairing thread) ────────────────

    def push(self, item: T) -> bool:
        """Enqueue `item`. Returns True on success, False on drop (queue full).

        Drops are logged per-occurrence and counted. Crossings of the HIGH
        threshold are logged as HIGH / RECOVERED events.
        """
        with self._lock:
            qsize_before = self._q.qsize()

            try:
                self._q.put_nowait(item)
            except queue.Full:
                self.dropped_total += 1
                log_encoding_buffer_drop(
                    self.name, self.dropped_total, qsize_before, self.capacity,
                )
                return False

            qsize_after = qsize_before + 1
            if qsize_after > self.qsize_peak:
                self.qsize_peak = qsize_after

            # High-water transitions — log each crossing once.
            if qsize_after >= self.capacity - self._high_margin and not self._high_state:
                self._high_state = True
                log_encoding_buffer_high(self.name, qsize_after, self.capacity)
            elif qsize_after < self.capacity - self._high_margin - 1 and self._high_state:
                # Hysteresis: drop one extra slot below the trigger before saying RECOVERED.
                self._high_state = False
                log_encoding_buffer_recovered(self.name, qsize_after, self.capacity)

            return True

    def push_sentinel(self) -> None:
        """Push None as a stop signal. Best-effort — never blocks, never drops it."""
        try:
            self._q.put_nowait(None)
        except queue.Full:
            # If queue is full, consumer will exit on _stop_event check anyway.
            pass

    # ── consumer side (called from encoder thread) ────────────────────────

    def get(self, timeout: float = 1.0) -> Optional[T]:
        """Pop the next item. Returns None on timeout OR on stop sentinel.

        Caller distinguishes timeout vs. sentinel via their own stop_event.
        """
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    # ── status (for HTTP /status and stats heartbeat) ─────────────────────

    def qsize(self) -> int:
        return self._q.qsize()

    def is_high(self) -> bool:
        return self._high_state

    def stats(self) -> dict:
        return {
            "name": self.name,
            "qsize": self._q.qsize(),
            "capacity": self.capacity,
            "qsize_peak": self.qsize_peak,
            "dropped_total": self.dropped_total,
            "high_state": self._high_state,
            "high_threshold": self.capacity - self._high_margin,
        }
