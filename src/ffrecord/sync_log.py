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
    hw_ref_time_in_frame: int,
    tc_str: str,
    timescale: int,
    flags: int,
    queue_depth: int,
    audio_qdepth: int,
) -> None:
    _sync.info(
        "[sync] video n=%d stream_time=%d hw_ref=%d hw_ref_valid=%s hw_ref_in_frame=%d "
        "tc=%s ts=%d flags=0x%08x qdepth=%d audio_qdepth=%d",
        frame_n, stream_time, hw_ref_time, hw_ref_valid, hw_ref_time_in_frame,
        tc_str, timescale, flags, queue_depth, audio_qdepth,
    )


def log_psf_frame() -> None:
    _sync.info("[sync] FRAME_FLAG psf=True")


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


def log_missed_frames(
    gap_frames: int,
    expected_stream_time: int,
    actual_stream_time: int,
    mitigation: str,
) -> None:
    _sync.warning(
        "[sync] MISSED_FRAMES gap=%d expected_pts=%d actual_pts=%d mitigation=%s",
        gap_frames, expected_stream_time, actual_stream_time, mitigation,
    )


def log_dropped_frame(output_name: str, total_dropped: int) -> None:
    _sync.warning(
        "[sync] DROPPED output=%s total=%d mitigation=output_queue_overflow",
        output_name, total_dropped,
    )
