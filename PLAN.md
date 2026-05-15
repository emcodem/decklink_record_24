# ffrecord — Final Plan

## Context

We are scoping a new sibling project at `C:\dev\ffrecord`, derived from ffcapture's DeckLink capture core but stripped of GUI/overlay/subtitle concerns. ffrecord is a **headless 24/7 SDI recording service**: one instance per SDI channel, capturing from DeckLink and writing to N user-configurable parallel outputs (mix of HLS preview, NVENC-encoded MOV/MP4/MXF files, etc.). Each output has its own codec, container, segment policy, audio config, and path template — all driven by YAML.

The operator needs deep A/V-sync and timestamp diagnostics with 7-day rolling per-channel logs to make post-mortem analysis possible after long-running incidents. Evidence from reading ffcapture's source confirmed the COM/native DeckLink path is the only one that can deliver those diagnostics; PyAV's `decklink` demuxer surfaces only pts/time_base. This plan therefore drops PyAV capture entirely and uses COM as the sole capture path (extended with SDK calls ffcapture doesn't currently exercise). PyAV stays for encoding/HLS muxing.

The handoff document at `C:\dev\ffcapture\HANDOFF_NEWPROJECT.md` captures decisions 1–21. This plan resolves the remaining open question (capture path) plus the smaller items left in §5.2, and lays out the concrete project skeleton.

## Goals

- Create `C:\dev\ffrecord` as an independent codebase that captures one DeckLink SDI input and writes to N parallel outputs.
- 24/7 operation with auto-reconnect on signal loss, per-output encoder auto-restart, disk-full pause/resume, and clean handoff to an external watchdog for hard-crash recovery.
- Deep A/V-sync diagnostics: per-frame hardware timestamps, embedded SMPTE timecode, video/audio arrival lag, format-change events, queue-depth telemetry. All logged to a 7-day rotating per-channel file.
- Operator control via signals (HUP/TERM) and a small HTTP server (read-only status + global pause + per-output enable/disable).

## Decisions (consolidated)

| Topic                          | Decision                                                                                                                                  |
| ------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------- |
| Project name / path            | `ffrecord` at `C:\dev\ffrecord`                                                                                                           |
| Relationship to ffcapture      | New sibling project, copy what's needed, no shared dep                                                                                    |
| Capture path                   | **COM/native primary, PyAV dropped entirely from capture.** PyAV remains for encoding.                                                    |
| Capture extensions vs ffcapture| Add `GetHardwareReferenceTimestamp`, `IDeckLinkTimecode` (SMPTE timecode), input queue depth telemetry, dropped-frame counters from SDK   |
| Encoder                        | PyAV in-process (NVENC-enabled libav build required on host)                                                                              |
| Output count                   | User-defined list of N outputs per service instance                                                                                       |
| Output codecs/containers       | NVENC h264/hevc into MOV/MP4/MXF; HLS for preview. All encoder params from YAML.                                                          |
| Interlaced handling            | 25i → 50p deinterlace **once at capture**; all outputs receive 50p                                                                        |
| Audio                          | Per-output channel selection + optional downmix from embedded SDI audio                                                                   |
| HLS muxing                     | PyAV's libav HLS muxer (`format='hls'`, `hls_list_size=2`, `hls_flags=delete_segments`)                                                   |
| Segment policy                 | Per-output (duration, etc.)                                                                                                               |
| Path template                  | Per-output, default `{output}/{CH}/{YYYYMMDDHH}/{starttime_unix_ms}.mov`                                                                  |
| Signal loss                    | Close current segment cleanly, leave a gap, start new segment on signal return                                                            |
| Format change mid-stream       | Treated as signal-loss event: close all segments, reinit pipeline, start new segments with new format                                     |
| Lifecycle                      | Always-on capture; HTTP exposes global pause/resume **and** per-output enable/disable                                                     |
| Control plane                  | Signals (HUP reload, TERM shutdown) + local HTTP server                                                                                   |
| HTTP library                   | Standard library `http.server` (zero deps)                                                                                                |
| Config format                  | YAML, one file per service instance                                                                                                       |
| Concurrency                    | Capture thread → per-output bounded queues → thread per output. Slow output drops from its own queue.                                     |
| Logging                        | Rotating log file per instance, 7-day rollover, plus stderr for supervisor                                                                |
| Retention                      | External watchdog (separate app) deletes oldest files. ffrecord only writes.                                                              |
| Process model                  | One binary per channel; external supervisor restarts whole process on hard crash                                                          |

## Architecture

### Capture (COM-only, extended)

Adapt from `ffcapture/src/decklink_com.py` and `decklink_comtypes.py`. Beyond what ffcapture exercises today, add:

- `IDeckLinkVideoInputFrame::GetHardwareReferenceTimestamp` per frame (logged at `[pts_diag]` level).
- `IDeckLinkVideoInputFrame::GetTimecode(bmdTimecodeRP188Any)` → SMPTE timecode string in frame metadata.
- Input queue depth via SDK (read after each callback; log statistics every N frames).
- Dropped-frame counters from `VideoInputFrameArrived` flags (`bmdFrameHasNoInputSource`, etc.).
- `VideoInputFormatChanged` callback already implemented in ffcapture — wire it to ffrecord's "treat as signal-loss" handler.

Deinterlace 25i → 50p once at capture, before fan-out. Use libav's `bwdif` or `yadif` filter in a single PyAV graph fed by the COM capture's BGRA/UYVY frames.

### Fan-out

```
COM capture thread
  → deinterlace filter (single instance)
  → fan-out: push (video_frame, audio_packet, hw_ts, sw_ts, tc) onto each output's bounded queue
```

Each output queue is bounded (e.g. `maxsize=10`). On `queue.Full`, the capture thread does **not** block — it drops for that output and increments a per-output drop counter. The capture thread's only job is to never stall.

### Output threads

One thread per configured output. Each holds:
- A PyAV `OutputContainer` (or a thin wrapper for HLS).
- An NVENC `CodecContext` for video (config-driven).
- An audio `CodecContext` and per-output channel-selection / downmix logic.
- Its own segment policy (duration trigger, etc.) and path template renderer.
- Crash isolation: a try/except around the encode loop; on encoder death, the thread tears down its container, logs the failure, and reopens a new segment.

### Segment rollover

When a segment's duration elapses (or on signal-loss / format-change), the output thread:
1. Flushes the encoder.
2. Closes the container.
3. Renders the new path from the template using the **next frame's timestamp**.
4. Opens a new container.
5. Continues encoding.

Rollover happens inside the output thread, so it never blocks the capture thread. The capture thread is unaware of segments.

### Control plane

- Signals (`SIGHUP`, `SIGTERM`) handled in `main.py`. `SIGHUP` re-reads YAML and applies safe changes (per-output enable/disable, segment duration). Codec/container changes require a full restart — logged and ignored at runtime.
- HTTP server in a background thread using stdlib `http.server.ThreadingHTTPServer`. Endpoints:
  - `GET /status` — JSON with capture state, per-output state, drop counters, last error per output, disk free.
  - `POST /pause` / `POST /resume` — global write pause (capture continues, outputs stop writing).
  - `POST /outputs/{name}/enable` / `disable`.
  - `GET /healthz` — for external supervisors.

### Logging

Two handlers on the root logger:
- `RotatingFileHandler` (or `TimedRotatingFileHandler`, 7-day rollover) writing to `logs/ffrecord_{channel}.log`.
- `StreamHandler` to stderr for the supervisor.

Per-component logger names: `ffrecord.capture`, `ffrecord.output.<name>`, `ffrecord.http`, `ffrecord.sync`. The `ffrecord.sync` logger is dedicated to A/V timestamp diagnostics so the operator can filter post-mortem.

## Directory structure

```
C:\dev\ffrecord\
├── pyproject.toml          # PyAV, PyYAML, comtypes; no PyQt6, no opencv
├── PLAN.md                 # This file
├── config\
│   └── example.yaml        # Documented example with all output types
├── src\ffrecord\
│   ├── __init__.py
│   ├── main.py             # Entry: parse args, load config, set up logging, install signal handlers, start service
│   ├── service.py          # Service class — lifecycle, supervises capture + outputs + http
│   ├── config.py           # YAML loader + dataclasses (CaptureConfig, OutputConfig, ServiceConfig)
│   ├── capture\
│   │   ├── __init__.py
│   │   ├── decklink_com.py # Adapted from ffcapture, extended with HW timestamps / timecode / queue depth
│   │   ├── decklink_comtypes.py  # Copied from ffcapture
│   │   └── deinterlace.py  # PyAV filter graph wrapper
│   ├── output\
│   │   ├── __init__.py
│   │   ├── base.py         # OutputThread abstract base + queue handling + segment policy
│   │   ├── file_output.py  # MOV/MP4/MXF segmented file output
│   │   ├── hls_output.py   # HLS rolling-window output (PyAV hls muxer)
│   │   └── path_template.py# Template renderer with {output}/{CH}/{YYYYMMDDHH}/{starttime_unix_ms} support
│   ├── http_server.py      # stdlib http.server wrapper, endpoints listed above
│   ├── logging_setup.py    # Rotating file + stderr setup
│   └── sync_log.py         # Helpers for the ffrecord.sync logger (per-frame hw_ts, sw_ts, tc, lag)
└── logs\                   # Created at startup if absent
```

## Files extracted from ffcapture (adapt, do not symlink)

- `src/decklink_com.py` → `src/ffrecord/capture/decklink_com.py` — extend with HW timestamp / timecode / queue depth.
- `src/decklink_comtypes.py` → `src/ffrecord/capture/decklink_comtypes.py` — copy as-is initially.
- `src/pipeline.py` — reference only. ffrecord's `service.py` is structurally similar but simpler (no overlay, no GUI, no playout).
- `src/outputs.py` — reference only. ffrecord's per-output thread design is more isolated; do not import directly.

**Not extracted:** `gui.py`, `overlay.py`, `config_subtitles.py`, `capture_pyav_decklink.py`, `decklink_native.py` (dead), `decklink_sdk_ctypes.py` (dead), `playout.py`.

## Implementation order

1. Project skeleton: directory layout, `pyproject.toml`, empty `main.py` that loads YAML and exits.
2. Logging setup (rotating file + stderr + named loggers).
3. Adapt `decklink_com.py` + `decklink_comtypes.py` into the new layout. Get a single-channel capture loop running that logs hw timestamps and format-change events.
4. Add SDK extensions: `GetHardwareReferenceTimestamp`, timecode, queue depth.
5. Deinterlace filter wrapper.
6. `OutputThread` base + bounded queue + segment-rollover skeleton.
7. `FileOutput` (MOV first; MP4 and MXF reuse the same machinery with different container/codec).
8. `HlsOutput` using PyAV's hls muxer.
9. Path template renderer.
10. HTTP server endpoints (`/status`, `/healthz`, `/pause`, `/resume`, per-output toggles).
11. Signal handlers (HUP reload of safe-to-change config, TERM graceful shutdown).
12. Signal-loss handler: close all segments, reinit, resume.
13. Format-change handler reuses signal-loss machinery.
14. Disk-full pause/resume.

## Verification

End-to-end checks once each milestone lands:

- **Capture loop alone (after step 4):** start the binary with a stub YAML that has no outputs. Confirm hw_ts, sw_ts, timecode lines appear in the sync log at the expected rate. Pull the SDI cable; confirm signal-loss event fires. Replug; confirm signal-return event fires. Switch SDI source to a different format; confirm `VideoInputFormatChanged` fires with old→new details.
- **Single MOV output (after step 7):** record for 30 minutes with 10-minute segments. Verify three files exist with names matching the template, each opens cleanly in VLC/ffprobe, A/V is in sync at the end of each segment, and gaps in the sync log correlate with reality.
- **HLS preview (after step 8):** point a browser at `http://localhost/<hls path>` (via a local static server) and verify live playback updates as segments roll. Confirm `hls_list_size=2` is enforced — playlist never grows past 2 segments and old `.ts` files are deleted.
- **Multi-output stress (after step 9):** run config with 1 HLS + 2 file outputs simultaneously for 4+ hours. Compare per-output drop counters in `/status`. Confirm none exceed a single-digit per-hour rate under normal load.
- **Resilience (after step 12):** kill an output encoder externally; confirm it restarts within seconds and other outputs are unaffected. Force signal loss during a segment; confirm clean close + gap + new segment on return.
- **Logging review:** after 24 h of operation, grep the rotating log for `[pts_diag]`, A/V sync warnings, format-change events. Confirm the operator can reconstruct the timeline of any incident from the log alone.
- **24/7 soak (post-MVP):** run for one full week with rotating logs enabled. Verify log rotation hits day-7 cleanly without breaking the active log handle. Verify memory and FD counts are stable.

## What this plan does NOT cover

- The external retention watchdog (separate app, scoped separately).
- A multi-channel orchestrator. Each ffrecord process handles one channel; running N channels means launching N ffrecord processes via the supervisor.
