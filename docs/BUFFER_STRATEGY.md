# Buffer & A/V Pairing Strategy

This document describes how video frames and audio packets flow from the
DeckLink capture callback to the encoder threads, and why the system is
structured the way it is. Read alongside `mitigations.md` for the failure
modes that this design addresses.

## Background — the problem this solves

DeckLink delivers a video frame and one audio packet in each
`VideoInputFrameArrived` callback. Each carries an independent 10 MHz
hardware timestamp (`hw_pts`). In the original design, video frames went
into one bounded queue, audio packets into another (8× larger), and the
encoder loop ran:

```
loop:
    take 1 video frame from video queue
    drain ALL audio packets currently sitting in the audio queue
    encode both with simple sequential PTS counters
```

This was simple but produced a per-segment A/V offset of 80–120 ms that
shifted by ~40 ms at random segment boundaries. The offset = audio
queue depth at the moment the first video frame of the new MOV segment
was encoded, divided by sample rate. Each new segment locked in whatever
queue depth happened to be present.

Across hours of recording the segment boundary at which the offset
shifted appeared arbitrary, and the shift was always audible in vistek
analysis.

## High-level design

```
DeckLink callback (decklink_com.py)
        ↓ (separate frame_callback + audio_callback)
Service._on_video_frame  Service._on_audio_packet
        ↓                        ↓
        └────────┬───────────────┘
                 ↓
         CaptureBuffer  (capture_buffer.py)
                 ↓ (emits complete AVPair objects via callback)
         Service._emit_pair  →  fan-out to each enabled output
                 ↓
   OutputThread.push_pair (output/base.py)
                 ↓
        EncodingBuffer[AVPair]  (encoding_buffer.py)  — one per output
                 ↓
        Encoder thread:  pair = self._get_pair()
                         encode pair.video, encode pair.audio
```

Two buffers, each with a distinct purpose:

| Buffer | Scope | Purpose | Size |
|---|---|---|---|
| `CaptureBuffer` | One per channel (Service-scoped) | Match audio samples to video frames by `hw_pts` before fan-out | byte-bounded, up to 2 GB of buffered video |
| `EncodingBuffer` | One per OutputThread | Backpressure between pairing layer and encoder thread | 500 pairs ≈ 10 s @ 50 fps |

Output PTS values inside MOV/HLS files are still zero-based per segment
— `hw_pts` is used only to decide which audio samples go with which
video frame, never to set the encoder's PTS. This preserves the
deterministic 30-second segment durations the old code already
guaranteed; the only thing that changes is *which audio* gets encoded
alongside each video frame.

## CaptureBuffer — pairing rules

Defined in `src/ffrecord/capture_buffer.py`. All four rules are policy
choices that were settled explicitly with the operator and should not be
silently changed.

### Rule 1 — Stale audio is discarded

If an audio packet arrives whose entire time range (`[hw_pts, hw_pts +
duration)`) ends before the oldest buffered video frame's `hw_pts`, the
packet is dropped and `AV_PAIR_STALE_AUDIO_DROPPED` is logged at WARNING
per occurrence. This eliminates the historic 80–120 ms offset: any
audio captured before the first video frame of a recording cannot be
paired and must not be allowed to influence the segment timeline.

### Rule 2 — Video is held until matching audio is provable

For each pending video frame `V` with range `[V.hw_pts, V.hw_pts +
frame_duration)`, emission is held until **any** of the following are
true:

- An audio packet arrives whose `hw_pts >= V.hw_pts + frame_duration`,
  proving that V's audio window is fully bounded by already-received
  audio (we now have enough data to extract V's slice). This is the
  steady-state case.
- An audio packet arrives whose `hw_pts >= V.hw_pts + frame_duration`
  but whose start is past V's end → V's audio is presumed lost (rule 3).
- The buffer byte budget is exceeded → forced emission with silence
  (rule 4).

### Rule 3 — Catch-up with silence for skipped frames

When audio arrives for a later video frame than the oldest pending one,
the older video frames cannot get real audio (audio arrives in order
from DeckLink). They are emitted with **synthesized silence** matching
the most-recent real audio's channel count and sample rate. Logged as
`AV_PAIR_CATCHUP_SILENCE` at WARNING per occurrence.

This rule was chosen explicitly over dropping the video frames: keeping
the video flowing — even with silent audio — is preferable to a visible
glitch.

### Rule 4 — Buffer cap forces emission

The total bytes of pending video data are capped at `BUFFER_MAX_BYTES =
2 GiB`. When the cap is exceeded, the oldest video frame is emitted with
synthesized silence (`AV_PAIR_FORCED_SILENCE`, WARNING). Approaching
75 % of the cap triggers `AV_PAIR_BUFFER_HIGH` once (edge-triggered).

At 1920×1080 yuv420p (~3.1 MB/frame) this is ~670 frames ≈ 13 s of
video held without matching audio. In practice the buffer stays at 0–2
frames in steady state.

### Audio slicing

A single audio packet often spans multiple video frames (DeckLink
delivers 1920 samples = 40 ms = two 50 fps frames per callback when the
input is 25 fps interlaced and `yadif=mode=1` doubles to 50 fps). The
extraction logic in `_extract_audio_range`:

1. Walks `_audio_buf` from the front
2. For each packet, computes its time range `[hw_pts, hw_pts +
   samples/sample_rate * 10_000_000)`
3. Slices the numpy sample array at exact tick boundaries
4. If a packet still has unconsumed samples for the *next* frame, it is
   replaced with a trimmed copy whose `hw_pts` is updated to where the
   trim ended
5. If any gap is detected (audio missing in the middle of V's range),
   the partial extraction is discarded and silence is used instead
   (`AV_PAIR_AUDIO_GAP`, WARNING)

The slicing always pads the final emitted audio to exactly
`frame_duration_samples`, so output audio duration = output video
duration per segment, byte for byte.

## EncodingBuffer — per-output backpressure

Defined in `src/ffrecord/encoding_buffer.py`. Generic
`EncodingBuffer[T]` wrapping `queue.Queue` with:

- `capacity = 500` (DEFAULT_QUEUE_CAPACITY), giving ≥ 8 s of headroom at
  any common framerate (10 s at 50 fps, 16.7 s at 30 fps, 20 s at 25
  fps)
- Drop policy: when full, incoming pair is discarded and counted
  (`stats.frames_dropped` + `ENC_BUF_DROP` WARNING per occurrence)
- High-water marker: when qsize ≥ capacity − 10 %, emit `ENC_BUF_HIGH`
  WARNING; below threshold with hysteresis, emit `ENC_BUF_RECOVERED`
  INFO. Edge-triggered so log volume stays low even under sustained
  encoder lag.

Memory: pairs hold references to numpy arrays. Multiple outputs of the
same channel share the underlying frame data (Python passes refs, not
copies). Worst-case per-output queue ≈ 500 × 3.1 MB ≈ 1.5 GB; shared
across outputs, real allocated memory ≈ unique frames in flight ×
frame size.

## Logging — every event, every time

The user's instruction was explicit: per-occurrence logging, no
graduated/throttled filters. Reduce verbosity later by raising
thresholds, not by sampling.

The pairing events live in `sync_log.py` under the `[av_pair]` and
`[enc_buf]` prefixes:

```
[av_pair] FIRST_PAIR video_hw_pts=… audio_hw_pts=… delta_us=…   INFO   (once per session)
[av_pair] EMIT video_hw_pts=… audio_hw_pts=… samples=… …       DEBUG  (every pair)
[av_pair] STALE_AUDIO_DROPPED audio_hw_pts=… samples=… …       WARNING
[av_pair] AUDIO_GAP video_hw_pts=… expected=N got=M …          WARNING
[av_pair] CATCHUP_SILENCE matched_audio_hw_pts=… …             WARNING
[av_pair] FORCED_SILENCE video_hw_pts=… buffered_bytes=… …     WARNING
[av_pair] BUFFER_HIGH pending_v=N pending_a=M bytes=B …        WARNING  (edge-triggered)
[av_pair] FORMAT_CHANGE_DROP pending_v=N pending_a=M           INFO

[enc_buf] DROP output=… total=N qsize=Q/Cap                    WARNING  (every drop)
[enc_buf] HIGH output=… qsize=Q/Cap                            WARNING  (edge-triggered)
[enc_buf] RECOVERED output=… qsize=Q/Cap                       INFO
```

Grep recipes:

```
# Every pairing decision and anomaly for one channel
grep -h '\[av_pair\]' logs/ch03/ffrecord_CH03.log

# Encoder backpressure across all channels
grep -h '\[enc_buf\]' logs/*/ffrecord_*.log
```

## Status API — real-time visibility

`Service.get_status()` returns (in addition to the pre-existing fields):

```json
"pairing": {
  "pending_video_frames": 0,
  "pending_audio_packets": 0,
  "buffered_video_bytes": 0,
  "buffered_video_mb": 0.0,
  "emitted_pairs_total": 12345,
  "stale_audio_drops_total": 0,
  "audio_gaps_total": 0,
  "catchup_silence_frames_total": 0,
  "forced_silence_frames_total": 0,
  "last_pair_delta_us": 0,
  "jitter_window_min_us": -20,
  "jitter_window_max_us": 20
},
"outputs": [
  {
    "name": "archive",
    "synthesized_audio_frames": 0,
    "pair_queue": {
      "name": "CH03/archive",
      "qsize": 0,
      "capacity": 500,
      "qsize_peak": 5,
      "dropped_total": 0,
      "high_state": false,
      "high_threshold": 450
    },
    ...
  }
]
```

`last_pair_delta_us` is the live A/V sync diagnostic: it's the
difference between paired audio and video `hw_pts` in microseconds. In
steady state it should be 0 ± a fraction of a frame.

`jitter_window_min_us` / `_max_us` cover the last 1000 pairs (~20 s at
50 fps) and reveal any drift over time.

## Known constant offset — filter pipeline delay

`yadif=mode=1` (used in the standard 1080i25 → 1080p50 config) buffers
one input frame for lookahead. When yadif yields its output frames in
callback N, those frames represent the visual content from callback
N-1. But the current `service.py` assigns them the `hw_pts` of callback
N — and the audio packet they pair with is also from callback N.

Result: a **constant 40 ms content offset** (audio content is 40 ms
later than video content). This is much better than the pre-redesign
variable 80–120 ms drift but is not yet true zero.

To drive this to zero, `InputVideoFilter` needs to expose its filter
delay so `service.py` can subtract one input-frame's duration from the
output frames' `hw_pts`. This refactor is deferred until a vistek run
confirms the constant offset matches expectations.

## Module map

| File | Responsibility |
|---|---|
| `src/ffrecord/capture_buffer.py` | `CaptureBuffer`: hw_pts pairing, slicing, silence synthesis, byte cap |
| `src/ffrecord/encoding_buffer.py` | `EncodingBuffer[T]`: generic bounded queue + drop / high-water logging |
| `src/ffrecord/output/base.py` | `VideoFrame`, `AudioPacket`, `AVPair` dataclasses; `OutputThread` + `EncodingBuffer[AVPair]` |
| `src/ffrecord/output/encoder.py` | Encoder loop consuming `AVPair` (MOV/MP4/MXF and HLS) |
| `src/ffrecord/service.py` | DeckLink callbacks → `CaptureBuffer` → fan-out to outputs |
| `src/ffrecord/sync_log.py` | `[av_pair]` and `[enc_buf]` logging helpers |

## Invariants worth preserving

If you modify any of this, keep these contracts intact:

1. **Output PTS stays counter-based and zero per segment.** Don't use
   `hw_pts` to set encoder PTS — that's what caused the
   variable-segment-duration problem the old code carefully avoided.
2. **Every emitted AVPair has `audio.data.shape[0] ==
   frame_duration_samples`**, real or silent. The encoder relies on this
   to keep audio and video durations bit-identical per segment.
3. **Stale audio is silently discarded; everything else is logged per
   occurrence.** Silent fallbacks elsewhere (e.g.
   `_frame_duration_ticks` with `fps_num <= 0`) all log at ERROR so
   misconfigurations show up immediately.
4. **`CaptureBuffer.reset()` on format change.** Pre-change pairs would
   be encoded with the wrong dimensions; the buffer must be flushed and
   the format-change-pending event passed through to outputs.
