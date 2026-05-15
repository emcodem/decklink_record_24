"""Helpers for the ffrecord.sync logger.

All A/V timestamp diagnostics go through this module so an operator can
  grep -h '\[sync\]' logs/ffrecord_CH1.log
to extract the full sync timeline without noise from other components.
"""

import logging

_sync = logging.getLogger("ffrecord.sync")


def log_video_frame(
    frame_n: int,
    stream_time: int,
    hw_ref_time: int,
    hw_ref_valid: bool,
    tc_str: str,
    timescale: int,
    flags: int,
    queue_depth: int,
) -> None:
    _sync.info(
        "[sync] video n=%d stream_time=%d hw_ref=%d hw_ref_valid=%s tc=%s "
        "ts=%d flags=0x%08x qdepth=%d",
        frame_n, stream_time, hw_ref_time, hw_ref_valid, tc_str,
        timescale, flags, queue_depth,
    )


def log_audio_packet(
    pkt_n: int,
    packet_time: int,
    hw_ref_time: int,
    hw_ref_valid: bool,
    timescale: int,
    sample_count: int,
) -> None:
    _sync.info(
        "[sync] audio n=%d packet_time=%d hw_ref=%d hw_ref_valid=%s ts=%d samples=%d",
        pkt_n, packet_time, hw_ref_time, hw_ref_valid, timescale, sample_count,
    )


def log_av_lag(frame_n: int, video_stream_time: int, audio_stream_time: int, timescale: int) -> None:
    lag_ticks = video_stream_time - audio_stream_time
    lag_ms = lag_ticks / timescale * 1000.0
    level = logging.WARNING if abs(lag_ms) > 40 else logging.DEBUG
    _sync.log(level, "[sync] av_lag n=%d lag_ms=%.1f", frame_n, lag_ms)


def log_signal_loss(reason: str) -> None:
    _sync.warning("[sync] SIGNAL_LOSS reason=%s", reason)


def log_signal_return(new_format: str) -> None:
    _sync.info("[sync] SIGNAL_RETURN format=%s", new_format)


def log_dropped_frame(output_name: str, total_dropped: int) -> None:
    _sync.warning("[sync] DROPPED output=%s total=%d", output_name, total_dropped)
