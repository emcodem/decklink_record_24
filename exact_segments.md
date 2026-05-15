# Plan: Force Exact Segment Duration via Frame Count

## Context
Recorded segments are consistently ~10 seconds but not exactly 10 seconds (observed range: 8.24–10.29s for a 10s target). The last clip is shorter by design (recording ended mid-segment). The 57 other clips vary by up to ±0.29s.

**Root cause:** `file_output.py` lines 162–164 use `time.monotonic()` for rollover:
```python
elapsed = time.monotonic() - segment_open_time
if elapsed >= self.cfg.segment_seconds:
```
The check fires on whichever video frame arrives *at or after* the wall-clock deadline. Capture/encode jitter means the segment overshoots by 1–several frames. Observed values (252, 253, 254 frames at 25fps for a 250-frame target) confirm this is overshoot, not random drift.

## Recommended Strategy: Frame-Count Rollover

Switch the rollover trigger from wall-clock elapsed time to video frame count. Since `seg_v_pts` increments by 1 per video frame and the stream `rate` is a `fractions.Fraction`, the exact frame count for `N` seconds is `round(N * rate)`. At 25fps: 250 frames = exactly 10.000s. At 24fps: 240 frames = exactly 10.000s.

This is the best simple strategy because:
- The `seg_v_pts` counter already exists and is already used for video PTS
- The stream `rate` is already computed in `open_new_segment`
- One condition swap, no new state machine, no audio-pipeline changes
- Determinism: every segment in a session has identical video frame count and identical video-stream duration

### Alternatives considered (rejected)

| Approach | Why rejected |
|---|---|
| **Hardware PTS (DeckLink `hw_pts`)** | Most precise (anchored to 10 MHz hardware clock), and `hw_pts` is already captured in `decklink_com.py:175-180` but unused. Rejected because it requires routing `hw_pts` through to the encoder, tracking segment-start hw_pts, and dealing with PTS gaps on dropped frames. The benefit over frame-count is negligible when capture is stable. |
| **FFmpeg segment muxer (`-f segment`)** | Complete rewrite of the open/close logic; loses the explicit per-file path templating (`path_template.render`) that the current architecture relies on. |
| **Sample-aligned audio truncation at boundary** | Would require splitting audio packets at the exact sample boundary and carrying the remainder into the next segment. Complex, and the audio drift is already sub-millisecond in normal operation. |
| **Tighter wall-clock check (sleep until deadline)** | Adds latency, still suffers from frame-arrival jitter, and fights the existing pull-based loop architecture. |

## Critical File
`src\ffrecord\output\file_output.py`

## Changes (single file, ~10 lines)

### 1. Add `frames_per_segment` to nonlocal state (around line 63)
```python
frames_per_segment = 0        # add after seg_a_frames = 0
```

### 2. Compute `frames_per_segment` inside `open_new_segment` (after line 78 where `rate` is set)
```python
nonlocal seg_v_pts, seg_a_pts_samples, seg_a_frames, frames_per_segment
...
frames_per_segment = round(self.cfg.segment_seconds * float(rate))
```

### 3. Replace wall-clock rollover check (lines 162–164)
**Remove:**
```python
elapsed = time.monotonic() - segment_open_time
if elapsed >= self.cfg.segment_seconds:
```
**Replace with:**
```python
if seg_v_pts >= frames_per_segment:
```

### 4. Remove `segment_open_time` (no longer used)
- Remove declaration line 55
- Remove assignment in `open_new_segment` line 114
- Remove from `nonlocal` lists and `close_segment` cleanup line 145
- Keep `import time` — still used for `now_ms = int(time.time() * 1000)` on line 156

## Edge cases & caveats

- **Fractional frame rates (e.g. 29.97 = 30000/1001):** `round(10 * 30000/1001) = 300` frames = 10.010s. Every segment is exactly 10.010s — deterministic but not literally 10.000s. This is intrinsic to NTSC-family rates and is the expected behavior.
- **Audio duration vs video duration:** The MOV container reports `format=duration` as the longest stream. Video is now exactly `frames_per_segment / rate` seconds; audio drains opportunistically and may be a few hundred samples (~few ms) off. In practice, the audio queue is well-behaved and audio duration tracks video to within a single audio-packet's duration. If the user reports residual sub-10ms drift, a follow-up can sample-align the audio.
- **Video stall safety:** With wall-clock rollover, a stalled video stream would still close the segment. With frame-count, the segment stays open until either more video frames arrive or recording stops. The `_get_video(timeout=1.0)` loop already handles this gracefully — frames simply don't come, no file corruption — but the segment file will be longer than `segment_seconds` of wall-clock time. This matches the *content* duration, which is arguably more correct.

## Verification
Record for ~60 seconds, then check all archived clips:
```powershell
Get-ChildItem "C:\dev\ffrecord\recording\archive\CH1\*" -Filter "*.mov" |
  ForEach-Object { $d = & ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 $_.FullName; "$($_.Name): $d" }
```
Expected: every non-final clip reports the same duration to 6 decimal places (e.g. exactly `10.000000` at 25fps with `segment_seconds: 10`). The final clip is still shorter (partial segment at recording end).
