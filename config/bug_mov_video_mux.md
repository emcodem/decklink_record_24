# Bug: * → MOV video mux fails ~50% of frames

## Status: FIXED

## Symptom

Archive output (MOV container) dropped ~50% of video frames on every run:

```
[mux] FAILED output=archive total=N reason=video: ArgumentError: Invalid argument: '...mov' returned 22
  | libav[mov]: Application provided invalid, non monotonically increasing dts to muxer in stream 0: 512 >= 512 | errno=22
```

Audio (pcm_s24le → MOV) muxed 100%. HLS (libx264 → mpegts) muxed 100%.
Signal is clean 1080i50 PsF, exactly 25 fps (`GetFrameRate()` raw = frameDuration=1000, timeScale=25000).

## Verified root cause

**yadif emits frames with `time_base=1/50`** (field-rate timebase), with pts stepping 0,2,4,6…
`encoder.py` overwrote `av_frame.pts = seg_v_pts` (0,1,2,3…) but **left `av_frame.time_base=1/50`
unchanged**. The codec context has `time_base=1/25` (set by libav after first encode; None before).
`stream.encode()` rescales frame pts from 1/50→1/25 (÷2), collapsing our monotonic pts into pairs:
0,0,1,1,2,2… → duplicate DTS after the 1/12800 MOV mux rescale → EINVAL on every second packet.

Notes:
- `ticks_per_frame` is removed in libavcodec 62 (FFmpeg 8.0 / PyAV 17.0.1 on this machine).
  Earlier documentation of this bug cited a `ticks_per_frame=2` mechanism — that was incorrect.
- `cc.time_base` is **None** until `stream.encode()` is first called (libav lazy-finalises the
  codec context). The frame-side fix (`av_frame.time_base = cc.time_base`) therefore requires a
  codec-side pin that sets `cc.time_base` explicitly before any encode, otherwise AttributeError.

### Why HLS was immune (accidental, not deliberate)

HLS uses a `scale=320:-2` per-output filter. The filter's `to_ndarray()`/`from_ndarray()` call
discarded the 1/50 timebase from the pulled frame → unset timebase → no rescale → no duplicate DTS.
This was an accidental side-effect. Both encode paths now use the same explicit timebase fix.

## Fix applied

1. `src/ffrecord/output/util.py` `build_video_stream()`: after `add_stream`, pin codec context timing:
   ```python
   stream.codec_context.framerate = rate
   stream.codec_context.time_base = fractions.Fraction(fps_den, fps_num)  # e.g. 1/25
   ```
   This ensures `cc.time_base` is never None when the encoder loop reads it.

2. `src/ffrecord/output/output_filter.py` `OutputVideoFilter.process()`: yield the raw `av.VideoFrame`
   from the buffersink instead of converting to ndarray (which discarded timebase metadata).

3. `src/ffrecord/output/encoder.py` `_encoder_loop()`: collapsed Path A / Path B into one unified
   path. Both sources (direct av_frame and filter output) now produce `av.VideoFrame`. For each frame:
   ```python
   av_frame.pts = seg_v_pts
   av_frame.time_base = vstream.codec_context.time_base  # the fix
   ```

## Cleanup done

- Removed `_MUX_DIAG_BURST`, `_pkt_diag()`, and per-packet INFO burst from `mux_with_logging()`.
- Removed `avlog.set_skip_repeated(False)` from `logging_setup.py`.
- Restored `config/example.yaml` archive output to production: `hevc_nvenc`, `main10`, `p010le`,
  preset `p7`, gop `auto`.

## Run / repro

```
pip install -e .              # ffrecord package (one-time)
python -m ffrecord.main --config config\example.yaml
```
This dev box is the LIVE broadcast host across 8 DeckLinks — ask before freeing a device.
