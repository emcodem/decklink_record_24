# ffrecord — Session Handoff

**Date:** 2026-05-15  
**Previous working directory:** `C:\dev\ffcapture`  
**New working directory:** `C:\dev\ffrecord`

---

## What happened this session

A new project `C:\dev\ffrecord` was bootstrapped from scratch based on a multi-turn scoping conversation. The full implementation plan is in `PLAN.md` (same directory as this file).

The project is a **headless 24/7 SDI recording service** — one process per DeckLink channel, writing to N parallel user-configurable outputs (HLS preview, segmented MOV/MXF/MP4 via NVENC). No GUI, no overlay, no subtitles.

---

## Current state of the codebase

All skeleton files have been written. Nothing has been run or tested yet — there is no venv, no installed dependencies.

### Files written (all in `C:\dev\ffrecord\src\ffrecord\`)

| File | Status |
|------|--------|
| `main.py` | Skeleton — entry point, signal handlers, arg parsing |
| `service.py` | Skeleton — lifecycle, fan-out, disk monitor, HTTP API |
| `config.py` | Complete — YAML loader + typed dataclasses |
| `logging_setup.py` | Complete — 7-day rotating file + stderr |
| `sync_log.py` | Complete — dedicated `ffrecord.sync` logger for A/V diagnostics |
| `http_server.py` | Complete — stdlib ThreadingHTTPServer, 6 endpoints |
| `capture/decklink_com.py` | Skeleton — COM capture, extended with HW timestamps + timecode + queue depth |
| `capture/decklink_comtypes.py` | Complete — COM vtable definitions (includes IDeckLinkInput.GetAvailableVideoFrameCount, IDeckLinkVideoInputFrame GetStreamTime + GetHardwareReferenceTimestamp) |
| `capture/deinterlace.py` | Complete — PyAV bwdif/yadif filter graph, lazy-init |
| `output/base.py` | Complete — OutputThread base, bounded queue, encoder auto-restart |
| `output/file_output.py` | Skeleton — MOV/MP4/MXF segmented output |
| `output/hls_output.py` | Skeleton — HLS rolling-window output |
| `output/path_template.py` | Complete — template renderer |
| `pyproject.toml` | Complete — PyAV, PyYAML, comtypes, numpy |
| `config/example.yaml` | Complete — documented HLS + MXF example |

---

## Key architectural decisions (already made, do not re-debate)

- **Capture:** COM/native DeckLink SDK only. PyAV dropped from capture. PyAV used only for encoding.
- **Encoder:** PyAV in-process (requires NVENC-enabled libav build — operator's responsibility).
- **Deinterlace:** once at capture (bwdif), all outputs receive 50p.
- **Concurrency:** capture thread → per-output bounded queue (maxsize=10) → thread per output.
- **Signal loss:** close all segments cleanly, gap, start new segments on signal return.
- **Format change mid-stream:** treated same as signal loss.
- **HTTP:** stdlib `http.server.ThreadingHTTPServer`, read-only status + global pause + per-output enable/disable.
- **Config:** YAML, one file per service instance. Channel name (`channel.name`) is the `{CH}` placeholder in path templates.
- **Logging:** `TimedRotatingFileHandler` (7-day), stderr for supervisor, dedicated `ffrecord.sync` logger for A/V timestamp diagnostics.

---

## What to do next (in order)

### Step 1 — Create venv and install dependencies

```powershell
cd C:\dev\ffrecord
python -m venv venv
venv\Scripts\activate
pip install -e .
```

> **Note:** Requires an NVENC-enabled PyAV wheel. If the standard `pip install av` build lacks NVENC, you'll need to install a custom wheel or build from source with `--enable-nonfree`.

### Step 2 — Test config loading (no hardware needed)

```powershell
python -c "from ffrecord.config import load_config; c = load_config('config/example.yaml'); print(c)"
```

### Step 3 — List DeckLink devices

```powershell
ffrecord --list-devices
```

### Step 4 — Run capture loop with no outputs (hardware test)

Create a minimal `config/test_capture_only.yaml`:

```yaml
channel:
  name: CH1
  decklink_device_index: 0
capture:
  audio_channels: 8
  deinterlace: none
http:
  bind: 127.0.0.1
  port: 8081
logging:
  dir: logs
  file_rotation_days: 7
  level: DEBUG
outputs: []
```

Then run and watch the sync log:
```powershell
ffrecord --config config\test_capture_only.yaml
# In another terminal:
Select-String -Path logs\ffrecord_CH1.log -Pattern '\[sync\]'
```

Expected: `[sync] video` and `[sync] audio` lines at the signal's frame rate. Pull the SDI cable — expect `SIGNAL_LOSS`. Replug — expect `SIGNAL_RETURN`.

### Step 5 — Single file output test

Add one `archive_mxf` output to the test config. Record for 30+ minutes. Verify segments appear on disk with names matching the template, open cleanly in VLC/ffprobe, A/V in sync.

---

## Known issues / TODOs to fix before first real use

1. **`decklink_com.py` — timecode extraction is incomplete.** The `GetTimecode` call stores a raw `c_void_p`; the `_tc_string()` helper tries to call `GetComponents` on it but the interface isn't QueryInterface'd properly. This needs a proper `IDeckLinkTimecode` COM interface added to `decklink_comtypes.py` and a correct `QueryInterface` call. The rest of the capture pipeline works without timecode — it just logs an empty string.

2. **`file_output.py` — codec context width/height setting.** Currently sets `vstream.codec_context.width` on the first frame check but NVENC codecs may need width/height set at stream-add time. May need to pass geometry through `OutputConfig` or delay stream creation to first frame with explicit codec context open.

3. **`deinterlace.py` — filter graph format name.** Uses `av.video.format.VideoFormat('uyvy422').name` which may return `'uyvy422'` or `'UYVY422'` depending on PyAV version. Test and normalise.

4. **`hls_output.py` — playlist path template.** The `path_template` for HLS should end in `.m3u8`. The `ensure_parent` call in `path_template.py` will create the parent directory correctly, but the path must be a file path, not a directory. Verify config example is correct.

5. **Signal handlers in `main.py`.** `SIGHUP` is skipped on Windows (correct), but NSSM sends a Windows service stop event, not SIGTERM. For NSSM-managed deployments, confirm how the service stop signal reaches the Python process.

---

## Source material

- `C:\dev\ffcapture\HANDOFF_NEWPROJECT.md` — original scoping document (§5 now has a pointer here)
- `C:\dev\ffrecord\PLAN.md` — full implementation plan with architecture, decisions, verification checklist
- `C:\dev\ffcapture\src\decklink_com.py` — original COM capture (reference for extending timecode/HW timestamp calls)
- `C:\dev\ffcapture\src\capture.py` — `Frame` / `AudioSample` dataclass originals (superseded by `output/base.py:VideoFrame/AudioPacket`)
