# Handoff: Vistek / DeckLink A/V Sync Verification

## Goal

Verify that `CaptureBuffer` (the hw_pts-based A/V pairing layer) does not introduce
A/V sync errors. The tool for this is `debugging/decklink_vistek_analyzer.py`, which
can now run in two modes: bypassing the buffer (baseline) or routing through it
(`--capture-buffer`). Comparing the reported delay between the two modes reveals
any timing distortion the buffer introduces.

---

## Files changed this session

| File | What changed |
|---|---|
| `debugging/decklink_vistek_analyzer.py` | Added `--capture-buffer` flag; new `_on_av_pair` callback; `VideoFrame`/`AudioPacket` wrapping |
| `src/ffrecord/capture/decklink_com.py` | Audio gap detection: pts continuity check, size-change logging, absent-audioPacket detection |
| `src/ffrecord/sync_log.py` | Added `log_audio_sample_gap`, `log_audio_pts_overlap` |

The other modified files (`output/base.py`, `output/file_output.py`, `output/hls_output.py`,
`service.py`) were already dirty before this session — not touched here.

---

## How to run

### Baseline (no buffer — direct to detectors)
```
cd debugging
python decklink_vistek_analyzer.py --device-index 0 --audio-channels 8
```

### With CaptureBuffer in the path
```
python decklink_vistek_analyzer.py --device-index 0 --audio-channels 8 --capture-buffer
```

Both modes print the same output format:
```
00:00:12.340  A/V delay: +  42.0 ms  (silence@12.340s  cross@12.298s  via silence)
```

Other useful flags:
- `--csv` — machine-readable output, easy to diff two runs
- `--duration 60` — stop after N seconds automatically
- `--debug` — prints raw luma + audio RMS every frame (very verbose)
- `--log-level DEBUG` — also shows CaptureBuffer's `[av_pair]` and `[sync]` events

---

## What to look for

### A/V delay comparison
Run both modes against the same Vistek signal. The reported `A/V delay` values should be
the same within ~1 frame duration (20 ms at 50fps). A larger difference indicates the
buffer is misattributing audio to the wrong video frame.

### Audio gap warnings in the log
The new capture-layer logging emits warnings you can grep:
```
grep "\[sync\] AUDIO" logs/...
```

| Tag | Meaning |
|---|---|
| `AUDIO_GAP_CAPTURE` | Pts jump between consecutive packets — samples missing at DeckLink layer |
| `AUDIO_PTS_OVERLAP` | Pts went backwards — possible duplicate delivery |
| `AUDIO_SIZE_CHANGE` | Packet sample count changed (normal at fractional framerates if alternating by 1) |
| `MISSED_FRAMES` | Video frame gap detected (audio loss here is expected/handled) |

### CaptureBuffer pairing events
With `--log-level DEBUG` and `--capture-buffer`:
```
grep "\[av_pair\]" logs/...
```

| Tag | Meaning |
|---|---|
| `CATCHUP_SILENCE` | Audio arrived for a newer frame before an older one — older frame gets silence |
| `AUDIO_GAP` | Audio range for a video frame was incomplete |
| `STALE_AUDIO_DROPPED` | Audio packet older than oldest pending video — discarded |
| `FORCED_SILENCE` | Buffer memory cap (2 GB) hit — video emitted without its audio |

Any `CATCHUP_SILENCE` or `AUDIO_GAP` events during normal recording are suspect —
they directly cause synthesized-silence frames in the output.

---

## Key design decisions made this session

### `--capture-buffer` mode data flow
```
DeckLink callback
  → _on_video_frame  →  wrap bytes → VideoFrame  → CaptureBuffer.push_video()
  → _on_audio_packet →  wrap arr  → AudioPacket  → CaptureBuffer.push_audio()
                                                        ↓ (emit_callback)
                                               _on_av_pair(AVPair)
                                                  → LiveBlackCrossDetector
                                                  → LiveAudioSilenceDetector
```

Without `--capture-buffer` the callbacks feed the detectors directly, same as before.

### Lock ordering — no deadlock
`CaptureBuffer` calls `_on_av_pair` while holding its own internal lock.
`_on_av_pair` then acquires `self._lock` (the analyzer lock).
The capture callbacks (`_on_video_frame`, `_on_audio_packet`) in buffer mode call
`push_video/push_audio` WITHOUT holding `self._lock`, so there is no cycle.

### Synthesized-silence pairs are skipped for audio detection
`CaptureBuffer` fills missing audio with zeros. If those zeros were fed to
`LiveAudioSilenceDetector` they would falsely trigger a silence event. The
`_on_av_pair` handler checks `pair.audio_is_synthesized` and skips audio
feeding for those pairs.

### Audio timestamps in `--capture-buffer` mode
`CaptureBuffer` stores `audio_hw_pts = V.hw_pts` on every emitted pair (both real
and synthesized). So in buffer mode the audio silence timestamp is snapped to the
video frame boundary, not the original sample-accurate packet pts. This means
measured A/V delays in buffer mode are quantised to ~1 frame. Expected and correct —
it is what the buffer does.

### Audio gap detection tolerance (decklink_com.py)
The gap check uses:
```python
tol_ticks = 2 * TIMESCALE // 48000   # 2-sample tolerance ≈ 416 ticks
```
This absorbs integer-division rounding in `expected_pts` without masking real gaps.
48000 Hz is hardcoded because `EnableAudioInput` always requests `bmdAudioSampleRate48kHz`.
If that ever changes, the gap math, `audio_callback(…, 48000, …)`, and
`CaptureBuffer._extract_audio_range` all need updating.

### No sample loss from rounding
`CaptureBuffer._extract_audio_range` uses floor division for `end_idx` when splitting
a packet across a frame boundary. The remainder is stored with `hw_pts = overlap_end`
(the frame's v_end). On the next call, `cursor - stored_hw_pts` is 0 or 1 tick
(frame-duration rounding error), which always rounds to `start_idx = 0` — the split
sample is always recovered by the next frame. No samples are discarded.
The only place samples are intentionally discarded is `push_audio` stale-audio
detection, which is logged as `STALE_AUDIO_DROPPED`.

---

## Open / next steps

- **Actually run the comparison**: the code is ready but has not been tested against
  a live Vistek signal yet. Run both modes, collect CSV output, diff the delay values.
- **Check for CATCHUP_SILENCE during normal recording**: if the existing production
  recordings show `[av_pair] CATCHUP_SILENCE` events in the logs, that is the
  root cause of any A/V offset shifts at segment boundaries.
- **`_frame_duration_ticks` floor division at 29.97fps**: `hw_pts_rate * fps_den // fps_num`
  floors the true frame duration (333666.666… → 333666 ticks). This means `v_end` is
  1 tick short of the true frame boundary every other frame. The split-sample analysis
  above shows this is safe, but it is worth verifying experimentally at 29.97fps.
- **Unrelated dirty files**: `output/base.py`, `output/file_output.py`,
  `output/hls_output.py`, `service.py` all have unstaged changes from before this
  session. Review and commit separately.
