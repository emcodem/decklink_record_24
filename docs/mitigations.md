# Mitigation Strategies

This document lists every failure mode that `ffrecord` can encounter, what the
service does automatically, and what — if anything — requires operator action.

---

## Automatic mitigations

### 1. Format change / signal return

**Trigger:** DeckLink fires `VideoInputFormatChanged` (resolution, frame rate, or
scan mode changed), or `IDeckLinkNotification` reports a status change.

**What happens automatically:**
1. Streams paused (`PauseStreams`)
2. Video input re-enabled for the new mode (`EnableVideoInput`)
3. Stream buffers flushed (`FlushStreams`)
4. Streams restarted (`StartStreams`)
5. PyAV filter graph closed and re-initialised for the new geometry on the next frame
6. Each output's stale audio queue flushed (pre-change samples discarded)
7. Output waits up to 200 ms for the first post-change audio packet to arrive for
   A/V alignment before proceeding

**Log sequence:**
```
INFO  [sync] SIGNAL_RETURN format=HD1080i50 1920x1080 25/1 progressive
```

**Operator action:** None. If the format change is unexpected (e.g. wrong cable
plugged in) the recording continues in the new format; check the new segment file.

---

### 2. No-signal frames (`bmdFrameHasNoInputSource`)

**Trigger:** SDI signal present at the connector but the source is not transmitting
a valid signal (black burst, equipment muted, upstream loss).

**Mitigation:** `frame_passed_flagged` — the frame is passed to the encoder
as-is. The encoder receives raw (likely black) pixel data. The PTS sequence
remains continuous so the recording timeline is preserved.

**Log:**
```
WARNING  [sync] SIGNAL_LOSS reason=bmdFrameHasNoInputSource mitigation=frame_passed_flagged
```

**Why not skip the frame?** Skipping would introduce a PTS gap identical to a
missed frame and add complexity for no benefit in a continuous-record workflow.
Black frames in the file make outages visible during review.

**Operator action:** None unless the outage is prolonged. Check the upstream
source.

---

### 3. Missed frames (stream_time gap)

**Trigger:** A frame's `stream_time` is more than half a frame duration later
than the expected value based on the previous frame. The DeckLink driver did not
deliver one or more frames to the callback.

**Mitigation:** `pts_gap_tolerated` — no frame is inserted; the muxer receives
the next available frame with its actual PTS. The container's frame duration for
the gap interval is absorbed by the next frame, causing a brief skip on playback.

**Log:**
```
WARNING  [sync] MISSED_FRAMES gap=2 expected_pts=100400000 actual_pts=120800000 mitigation=pts_gap_tolerated
```

**Common causes:** DeckLink driver ring buffer overflowed (callback too slow),
or the hardware genuinely dropped a frame due to signal integrity problems.
Correlate with `DECKLINK_BUFFER_HIGH` to distinguish.

**Operator action:** If frequent, investigate CPU load during the callback or
reduce the number of active outputs.

---

### 4. Output queue overflow (encoder too slow)

**Trigger:** The output thread's video queue reaches capacity (10 frames). A
new frame from DeckLink cannot be enqueued.

**Mitigation:** `output_queue_overflow` — the incoming frame is discarded
immediately at `push_video()`. The encoder never sees it. The output file gets
a PTS gap at the point of the drop.

**Log sequence:**
```
WARNING  [sync] QUEUE_NEAR_FULL output=archive qsize=8/10 — encoder falling behind
WARNING  [sync] DROPPED output=archive total=1 mitigation=output_queue_overflow
WARNING  [sync] DROPPED output=archive total=10 mitigation=output_queue_overflow
[stats]  ... dropped=10 (+9) ... vq=10/10 ...
INFO     [sync] QUEUE_RECOVERED output=archive qsize=3/10
```

**Note:** Each output has its own independent queue. A slow HLS output dropping
frames does not affect the archive output.

**Operator action:**
- Check GPU utilisation; NVENC saturation is the most common cause.
- Reduce encoder bitrate, disable B-frames, or switch to a faster preset.
- Disable non-critical outputs (HTTP API: `POST /outputs/<name>/disable`) to
  relieve pressure on shared resources.

---

### 5. Encoder crash and restart

**Trigger:** Any unhandled exception in the encoder thread (PyAV error, muxer
failure, disk I/O error, NVENC driver fault).

**Mitigation:** The encoder thread sleeps 2 seconds (`RESTART_DELAY`), then
re-opens the container and resumes encoding. A new segment is started. The
2-second gap means the output queue fills and frames are dropped during recovery.

**Log:**
```
ERROR  Encoder crashed (restart #1): [Errno 28] No space left on device
INFO   Restarting encoder...
```

**Operator action:** Investigate the exception. A transient NVENC fault
self-recovers. A persistent error (disk full, permission denied, corrupted
container) will restart in a loop — each attempt is counted in `restarts=<N>`
in the `[stats]` heartbeat.

---

### 6. Disk full — pause and resume

**Trigger:** Free space on the recording volume falls below 5.0 GB
(`DISK_PAUSE_THRESHOLD_GB`). Checked every 30 seconds.

**Mitigation:** All outputs are paused. Frames continue arriving from DeckLink
but are discarded inside `push_video()` (no queue writes). Recording resumes
automatically when free space rises above 10.0 GB (`DISK_RESUME_THRESHOLD_GB`).
The hysteresis prevents rapid oscillation.

**Log:**
```
ERROR  [disk] Free space 3.2 GB below pause threshold 5.0 GB — pausing all writes
INFO   [disk] Free space 12.1 GB above resume threshold 10.0 GB — resuming writes
```

**Operator action:** Free disk space (delete old segments, expand volume). No
restart required; recording resumes automatically.

---

### 7. DeckLink driver buffer filling up

**Trigger:** `GetAvailableVideoFrameCount()` returns ≥ 3 frames waiting in the
DeckLink driver's ring buffer.

**Mitigation:** None applied automatically — this is an early warning. If the
buffer fills completely the driver silently drops frames before the callback is
called; those drops appear as `MISSED_FRAMES` events, not `DROPPED` events.

**Log:**
```
WARNING  [sync] DECKLINK_BUFFER_HIGH qdepth=4 — callback thread falling behind
INFO     [sync] DECKLINK_BUFFER_RECOVERED qdepth=1
```

**Operator action:** Investigate CPU load on the callback thread. Possible
causes: deinterlace filter is expensive, too many outputs receiving frames in
the fan-out loop, or system is swapping.

---

### 8. Audio driver buffer filling up

**Trigger:** `GetAvailableAudioSampleFrameCount()` returns > 4 800 samples
(> 100 ms at 48 kHz).

**Mitigation:** None. Audio continues arriving via callback; if the audio queue
in each output also fills, audio packets are silently dropped (`push_audio`
swallows `queue.Full` without counting).

**Log:**
```
WARNING  [sync] AUDIO_QUEUE_DEPTH qdepth=9600 (>100ms at 48kHz)
```

**Operator action:** Same as `DECKLINK_BUFFER_HIGH` — investigate callback
thread load. Also check whether the output threads are draining audio promptly;
a stalled encoder blocks audio drain.

---

### 9. GetStreamTime / GetPacketTime failures

**Trigger:** The DeckLink COM call to extract the frame or audio PTS fails.

**Mitigation:** The frame or packet is still forwarded to the encoder, but
`hw_pts_valid = False`. The encoder uses its own segment-local PTS counter
(incrementing by 1 per frame), so continuity is maintained within the segment.
A/V lag checks and `MISSED_FRAMES` detection are skipped for that frame.

**Log (graduated — fires at counts 1, 10, 100, 1 000, then every 1 000):**
```
WARNING  [av_sync_warn] GetStreamTime failed (#1): <COM error>
ERROR    [av_sync_warn] GetStreamTime failed (#1000): <COM error>
```

**Operator action:** If persistent, the DeckLink driver or firmware may need
updating. Sustained failures (count reaching 1 000+) escalate to ERROR.

---

### 10. Signal not locked (no frames arriving)

**Trigger:** `IDeckLinkStatus::GetFlag(bmdDeckLinkStatusVideoInputSignalLocked)`
returns False. Checked at startup (1 s after `StartStreams`) and every 30 s by
the health monitor.

**Mitigation:** None — the service logs and waits. `VideoInputFormatChanged`
will fire when the signal is restored, triggering normal format-change recovery.

**Log:**
```
WARNING  [sync] SIGNAL_LOSS reason=signal_not_locked (startup probe — no SDI signal detected)
WARNING  [sync] SIGNAL_LOSS reason=signal_not_locked (periodic health check — no SDI signal)
```

**Operator action:** Check SDI cable, upstream source, and DeckLink device
power. No restart required — recovery is fully automatic.

---

### 11. Progressive Segmented Frame (PsF) detection

**Trigger:** A frame arrives with the `bmdFrameCapturedAsPsF` flag set (bit 30).
The SDI signal is carrying progressive content encoded in a segmented interlaced
wrapper.

**Mitigation:** None — the frame is processed normally. The flag is logged for
operator awareness.

**Log:**
```
INFO  [sync] FRAME_FLAG psf=True
```

**Operator action:** If a deinterlace filter is configured and the input is PsF,
the filter will operate on what it sees as interlaced frames and will double
progressive lines — producing artefacts. Disable the deinterlace filter in
config for PsF sources.

---

## Manual-only interventions

The following conditions are not automatically recovered and require operator
action:

| Condition | How to detect | Action |
|-----------|--------------|--------|
| NVENC driver hung (encoder restart loop) | `restarts=N` growing in `[stats]`, same ERROR repeating | Restart the service; may require GPU driver reset |
| Disk full and no space to free | `[disk]` pause never clears | Expand volume or redirect output path |
| Wrong pixel format / unsupported mode | Encoder crash on format change | Check `SIGNAL_RETURN format=...` against supported modes in config |
| Per-output disabled by HTTP API | `enabled=False` in `[stats]` | `POST /outputs/<name>/enable` |
| Service paused by HTTP API | `paused=True` in `[stats]` | `POST /resume` |
