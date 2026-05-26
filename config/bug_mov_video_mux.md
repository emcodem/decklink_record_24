# Bug: * → MOV video mux fails ~50% of frames

## Symptom

Archive output with MOV container drops roughly half its video frames on every run:

```
[mux] FAILED output=archive total=N reason=video: ArgumentError: Invalid argument: '...mov' returned 22
```

Stats after ~5 s always show `video: encoded=111 muxed=56` (≈50%). Audio muxing succeeds 100%. HLS output with identical codec succeeds 100%.

## Reproduction

```
ffrecord --config config\example.yaml
```

Watch logs for `[mux] FAILED output=archive`.

## Tested configurations (all fail identically)

| codec | gop | result |
|---|---|---|
| hevc_nvenc | auto | ~50% muxed |
| h264_nvenc | auto | ~50% muxed |
| libx264 | auto | ~50% muxed |
| libx264 | 1 (all I-frames) | ~50% muxed |

## What works

- HLS output: libx264 → mpegts, `25/1` timebase → 100% muxed
- Audio: pcm_s24le → MOV → 100% muxed

## Hypothesis

Timebase mismatch in the MOV muxer. Archive encodes at `25000/1000`; HLS encodes at `25/1`. Both reduce to 25 fps but the MOV muxer may round packet DTS/PTS differently at the coarser timebase, causing every other packet to land on a duplicate or out-of-order timestamp → EINVAL.

gop=1 rules out frame-type (I vs P/B) and inline parameter set causes.

## Next step

Log `packet.pts`, `packet.dts`, `packet.time_base` in `mux_with_logging` for both the failing MOV video stream and the passing HLS video stream and compare the sequence. Look for duplicate or non-monotonic DTS on the failing side.
