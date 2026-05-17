# Log Reading Guide

## Log files

One rotating file per channel, written to the directory set in `logging.dir`:

```
logs/ffrecord_CH1.log          # current
logs/ffrecord_CH1.log.2026-05-17  # previous days, kept for logging.file_rotation_days
```

Stderr receives the same lines and is captured by supervisord / systemd.

Format of every line:

```
2026-05-17 14:23:01,042 WARNING  ffrecord.sync  [sync] SIGNAL_LOSS reason=bmdFrameHasNoInputSource ...
<timestamp>             <level>  <logger>        <message>
```

---

## Quick grep patterns

Extract the entire sync timeline (signal events, drops, frame diagnostics):

```powershell
Select-String '\[sync\]' logs\ffrecord_CH1.log
```

Extract only warnings and errors from the sync timeline:

```powershell
Select-String 'WARNING|ERROR' logs\ffrecord_CH1.log | Select-String '\[sync\]|\[disk\]|\[av_sync_warn\]|Encoder crashed'
```

Watch live:

```powershell
Get-Content logs\ffrecord_CH1.log -Wait | Select-String '\[sync\]|\[stats\]|\[disk\]|\[av_sync_warn\]|Encoder'
```

Count total drops in a log file:

```powershell
(Select-String 'DROPPED' logs\ffrecord_CH1.log).Count
```

Show only the 5-second stats heartbeats for one output:

```powershell
Select-String '\[stats\]' logs\ffrecord_CH1.log | Select-String 'output=archive'
```

---

## Buffer architecture

Understanding which buffer fired which event requires knowing the two-level pipeline:

```
SDI hardware
    │
    ▼
DeckLink driver ring buffer          ← hardware-managed, depth reported by
    │                                  GetAvailableVideoFrameCount()
    │  [sync] DECKLINK_BUFFER_HIGH fires here (depth ≥ 3)
    │
    ▼
VideoInputFrameArrived callback      ← copies frame bytes, runs on driver thread
    │
    ▼
push_video()  →  _video_queue        ← Python queue, max 10 frames per output
    │                                  [sync] QUEUE_NEAR_FULL fires at ≥ 8/10
    │                                  [sync] DROPPED fires when full
    ▼
Encoder thread (NVENC / software)
    │
    ▼
Muxer → segment file on disk
```

If `DECKLINK_BUFFER_HIGH` fires, the bottleneck is in the callback thread or
Python frame-processing code. If only `QUEUE_NEAR_FULL` fires, the callback is
fine and the encoder/muxer is the constraint.

At 25 fps the output queue holds **400 ms** of frames. At 50 fps **200 ms**,
at 60 fps **167 ms**.

---

## Log token reference

All diagnostic events use a bracketed tag so they can be extracted with a
single grep. The tag appears at the start of the message body.

### `[sync]` — A/V synchronisation and frame health

These events are emitted by `ffrecord.sync` and `ffrecord.capture`.

#### Startup diagnostics (first 20 frames/packets after each format change)

```
[sync] video n=<frame#> stream_time=<ticks> hw_ref=<ticks> hw_ref_valid=<bool>
       hw_ref_in_frame=<ticks> tc=<HH:MM:SS:FF> ts=10000000 flags=0x<hex>
       qdepth=<driver_frames> audio_qdepth=<driver_samples>
```
- `stream_time` — DeckLink stream clock (shared with audio, used for A/V sync)
- `hw_ref` — independent 10 MHz hardware wall clock
- `hw_ref_in_frame` — hardware clock offset within the current frame slot (sub-frame jitter)
- `tc` — SMPTE RP188 timecode from VBI; empty if not embedded
- `qdepth` — frames currently queued in the DeckLink driver ring buffer
- `audio_qdepth` — audio samples currently queued in the DeckLink driver buffer

```
[sync] audio n=<pkt#> packet_time=<ticks> hw_ref=<ticks> hw_ref_valid=<bool>
       ts=10000000 samples=<count>
```

After the first 20 frames/packets, these move to DEBUG level and disappear
from the log unless the log level is set to DEBUG.

#### Ongoing events (always emitted)

| Token | Level | Meaning |
|-------|-------|---------|
| `SIGNAL_LOSS reason=bmdFrameHasNoInputSource mitigation=frame_passed_flagged` | WARNING | A frame arrived with the no-signal flag set. Frame is passed to the encoder as-is (likely black). Fires once per frame — may be noisy during a long outage. |
| `SIGNAL_LOSS reason=signal_not_locked (startup probe ...)` | WARNING | At startup, 1 s after `StartStreams`, `IDeckLinkStatus` reports no signal lock. Check cable and source. |
| `SIGNAL_LOSS reason=signal_not_locked (periodic health check ...)` | WARNING | Periodic 30 s poll of `IDeckLinkStatus` found no signal. Fires even when frames have stopped arriving completely. |
| `SIGNAL_LOSS reason=signal_not_locked (IDeckLinkNotification)` | WARNING | `IDeckLinkNotification` fired a `bmdStatusChanged` event reporting signal unlock — supplementary to `VideoInputFormatChanged`. |
| `SIGNAL_RETURN format=<mode>` | INFO | DeckLink `VideoInputFormatChanged` fired with a new valid format. Streams restarted. |
| `STATUS_CHANGE signal_locked=True` | INFO | `IDeckLinkNotification` reported signal lock acquired. |
| `FRAME_FLAG psf=True` | INFO | Frame arrived with `bmdFrameCapturedAsPsF` — the signal is Progressive Segmented Frame encoded as interlaced. No mitigation applied; logged for operator awareness. If a deinterlace filter is configured this may produce doubled frames. |
| `MISSED_FRAMES gap=<N> expected_pts=<ticks> actual_pts=<ticks> mitigation=pts_gap_tolerated` | WARNING | DeckLink delivered a frame whose `stream_time` is more than 0.5 frame durations later than expected. `gap` is the estimated number of missing frames. The muxer will see a PTS jump; players handle this as a brief skip. |
| `av_lag n=<frame#> lag_ms=<ms>` | DEBUG / WARNING | Video stream_time minus most-recent audio stream_time. Emitted at WARNING when `abs(lag_ms) > 40`. A persistent lag indicates the audio and video clocks are drifting. |
| `DECKLINK_BUFFER_HIGH qdepth=<N>` | WARNING | The DeckLink driver ring buffer has ≥ 3 frames queued. The callback thread is processing frames slower than they arrive. If this persists the driver will start dropping frames before they reach Python. |
| `DECKLINK_BUFFER_RECOVERED qdepth=<N>` | INFO | Driver ring buffer depth dropped below threshold. |
| `AUDIO_QUEUE_DEPTH qdepth=<samples>` | WARNING | Driver audio buffer exceeds 4 800 samples (> 100 ms at 48 kHz). Audio latency is building up in the driver. |
| `QUEUE_NEAR_FULL output=<name> qsize=<N>/10` | WARNING | The Python output queue is ≥ 8 frames deep. The encoder is consuming frames slower than they arrive. Next log will be `DROPPED` if the queue fills. |
| `QUEUE_RECOVERED output=<name> qsize=<N>/10` | INFO | Output queue depth fell below the near-full threshold. |
| `DROPPED output=<name> total=<N> mitigation=output_queue_overflow` | WARNING | A frame was discarded because the output queue was full. Logged at cumulative counts 1, 10, 100, 1 000, then every 1 000. The `total` counter never resets within a run. |

### `[stats]` — encoder heartbeat (every 5 seconds, per output)

```
[stats] video: encoded=<total> (+<delta>, <fps> fps) muxed=<total> (+<delta>)
        dropped=<total> (+<delta>) | audio: encoded=<total> (+<delta>)
        muxed=<total> (+<delta>) | segments=<N> restarts=<N>
        vq=<qsize>/10 aq=<qsize>/80 enabled=<bool> paused=<bool>
```

Key fields:

| Field | Normal | Concern |
|-------|--------|---------|
| `+<fps> fps` | Equal to source framerate | Below source = encoder not keeping up |
| `dropped (+<delta>)` | `+0` always | Any positive delta = frames lost to queue overflow |
| `vq=<N>/10` | 0–2 | ≥ 8 = encoder falling behind |
| `aq=<N>/80` | 0–10 | Growing = audio not being drained |
| `restarts=<N>` | 0 | Any restart = encoder crashed (see ERROR lines) |

### `[disk]` — disk space monitor (every 30 seconds)

| Message | Level | Action taken |
|---------|-------|-------------|
| `[disk] Free space X.X GB below pause threshold 5.0 GB — pausing all writes` | ERROR | All outputs paused. Frames still arrive from DeckLink but are discarded in `push_video`. |
| `[disk] Free space X.X GB above resume threshold 10.0 GB — resuming writes` | INFO | All outputs resumed. |

### `[av_sync_warn]` — graduated API failure warnings

Emitted when a DeckLink COM call fails repeatedly. The count suffix shows how
many consecutive failures have occurred.

```
[av_sync_warn] GetStreamTime failed (#1): <exception>
[av_sync_warn] GetStreamTime failed (#10): <exception>
[av_sync_warn] GetStreamTime failed (#100): <exception>   ← level escalates to ERROR
```

Affected calls: `GetStreamTime`, `GetPacketTime`. When `GetStreamTime` fails
the frame's `stream_time` is set to 0 and marked invalid; the encoder receives
it with no hardware PTS.

### Encoder crash lines (no tag)

```
ERROR ffrecord.output.<name>  Encoder crashed (restart #<N>): <exception>
...traceback...
INFO  ffrecord.output.<name>  Restarting encoder...
```

The encoder thread sleeps 2 s then re-opens the container and resumes. Frames
that arrive during the restart window fill the queue and are dropped once it
fills.

---

## Diagnostic workflows

### Signal dropped mid-run

Look for the sequence:

```
WARNING  [sync] SIGNAL_LOSS reason=bmdFrameHasNoInputSource ...   ← first bad frame
WARNING  [sync] MISSED_FRAMES gap=N ...                           ← frames never delivered
INFO     [sync] SIGNAL_RETURN format=HD1080i50 ...                ← recovery
```

If only `SIGNAL_LOSS (periodic health check)` appears without a `SIGNAL_RETURN`,
the driver stopped delivering frames entirely (no callback fires) but signal lock
is still gone — the source may be permanently off.

### Encoder throughput problem

```
WARNING  [sync] QUEUE_NEAR_FULL output=archive qsize=8/10
WARNING  [sync] DROPPED output=archive total=1 ...
[stats]  video: encoded=125 (+22, 22.3 fps) ... dropped=1 (+1) ... vq=10/10
```

`fps` below source rate + growing `dropped` delta + `vq` near 10 = NVENC
saturated. Check GPU utilisation. If `DECKLINK_BUFFER_HIGH` also fires, the
CPU copy in the callback is the bottleneck.

### DeckLink callback bottleneck

```
WARNING  [sync] DECKLINK_BUFFER_HIGH qdepth=5
```

Without a corresponding `QUEUE_NEAR_FULL`: the Python callback thread is slow
(frame copy, deinterlace filter, or fan-out to multiple outputs). Check CPU
usage. Reduce output count or disable the deinterlace filter to confirm.

### Disk full

```
ERROR    [disk] Free space 3.2 GB below pause threshold 5.0 GB — pausing all writes
```

Recording stops. Free space, then it auto-resumes above 10 GB. No manual
restart required.
