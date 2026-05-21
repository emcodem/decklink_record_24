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
    level = logging.WARNING if abs(lag_ms) > 40 else logging.INFO
    _sync.log(
        level,
        "[sync] AV_LAG n=%d video_stream_time=%d audio_pts=%d lag_ms=%.2f",
        frame_n, video_stream_time, audio_stream_time, lag_ms,
    )


def log_signal_loss(reason: str) -> None:
    _sync.warning("[sync] SIGNAL_LOSS reason=%s", reason)


def log_signal_return(new_format: str) -> None:
    _sync.info("[sync] SIGNAL_RETURN format=%s", new_format)


def log_audio_sample_gap(
    pkt_n: int,
    gap_samples: int,
    gap_ticks: int,
    prev_pts: int,
    curr_pts: int,
    timescale: int,
) -> None:
    """Pts jump between consecutive audio packets implies missing samples at capture."""
    gap_ms = gap_ticks / timescale * 1000.0
    _sync.warning(
        "[sync] AUDIO_GAP_CAPTURE pkt_n=%d gap_samples=%d gap_ms=%.2f "
        "prev_pts=%d curr_pts=%d (samples missing at DeckLink layer)",
        pkt_n, gap_samples, gap_ms, prev_pts, curr_pts,
    )


def log_audio_pts_overlap(
    pkt_n: int,
    overlap_ticks: int,
    prev_pts: int,
    curr_pts: int,
    timescale: int,
) -> None:
    """Pts went backwards — possible duplicate or out-of-order audio delivery."""
    overlap_ms = abs(overlap_ticks) / timescale * 1000.0
    _sync.warning(
        "[sync] AUDIO_PTS_OVERLAP pkt_n=%d overlap_ms=%.2f prev_pts=%d curr_pts=%d",
        pkt_n, overlap_ms, prev_pts, curr_pts,
    )


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


# ── A/V pairing buffer (capture_buffer.CaptureBuffer) ──────────────────────
#
# Every event is logged individually for now. Reduce verbosity later by
# raising thresholds or batching once the steady-state pattern is understood.

def log_av_pair_first(video_hw_pts: int, audio_hw_pts: int, timescale: int) -> None:
    """One-time per recording session: the very first paired emission."""
    delta_us = (audio_hw_pts - video_hw_pts) * 1_000_000 // max(1, timescale)
    _sync.info(
        "[av_pair] FIRST_PAIR video_hw_pts=%d audio_hw_pts=%d delta_us=%d",
        video_hw_pts, audio_hw_pts, delta_us,
    )


def log_av_pair_emitted(
    video_hw_pts: int, audio_hw_pts: int, audio_samples: int,
    pending_video: int, pending_audio: int, synthesized: bool,
) -> None:
    """Every emitted pair (DEBUG)."""
    _sync.debug(
        "[av_pair] EMIT video_hw_pts=%d audio_hw_pts=%d samples=%d "
        "pending_v=%d pending_a=%d synthesized=%s",
        video_hw_pts, audio_hw_pts, audio_samples,
        pending_video, pending_audio, synthesized,
    )


def log_av_pair_stale_audio(
    audio_hw_pts: int, samples: int, oldest_video_hw_pts: int,
) -> None:
    """Audio packet older than the oldest pending video — discarded."""
    _sync.warning(
        "[av_pair] STALE_AUDIO_DROPPED audio_hw_pts=%d samples=%d "
        "oldest_video_hw_pts=%d (audio entirely before oldest video, discarded)",
        audio_hw_pts, samples, oldest_video_hw_pts,
    )


def log_av_pair_audio_gap(
    video_hw_pts: int, expected_samples: int, got_samples: int,
) -> None:
    """Pending video can be emitted but its audio range has a gap — filled with silence."""
    missing = expected_samples - got_samples
    _sync.warning(
        "[av_pair] AUDIO_GAP video_hw_pts=%d expected=%d got=%d missing=%d "
        "mitigation=fill_with_silence",
        video_hw_pts, expected_samples, got_samples, missing,
    )


def log_av_pair_forced_silence(
    video_hw_pts: int, buffered_video_bytes: int, pending_video: int,
) -> None:
    """Buffer cap forced emission of a video frame without its audio (audio still pending)."""
    _sync.warning(
        "[av_pair] FORCED_SILENCE video_hw_pts=%d buffered_bytes=%d pending_v=%d "
        "reason=buffer_cap_reached",
        video_hw_pts, buffered_video_bytes, pending_video,
    )


def log_av_pair_catchup_silence(
    video_hw_pts: int, audio_hw_pts: int, frames_caught_up: int,
) -> None:
    """One or more older video frames got silence because audio arrived for a newer frame."""
    _sync.warning(
        "[av_pair] CATCHUP_SILENCE matched_audio_hw_pts=%d older_video_hw_pts=%d "
        "frames_silenced=%d mitigation=emit_older_video_with_silence",
        audio_hw_pts, video_hw_pts, frames_caught_up,
    )


def log_av_pair_buffer_high(
    pending_video: int, pending_audio: int, buffered_bytes: int, threshold_bytes: int,
) -> None:
    """Buffer approaching cap — warn but don't act yet."""
    _sync.warning(
        "[av_pair] BUFFER_HIGH pending_v=%d pending_a=%d bytes=%d threshold=%d "
        "(approaching cap)",
        pending_video, pending_audio, buffered_bytes, threshold_bytes,
    )


def log_av_pair_format_change_drop(pending_video: int, pending_audio: int) -> None:
    """Format change forced full buffer flush — pre-change pairs would be invalid."""
    _sync.info(
        "[av_pair] FORMAT_CHANGE_DROP pending_v=%d pending_a=%d",
        pending_video, pending_audio,
    )


# ── EncodingBuffer (per-output bounded queue) ──────────────────────────────

def log_encoding_buffer_drop(output_name: str, total_dropped: int, qsize: int, qmax: int) -> None:
    """Per-output queue full — incoming pair dropped."""
    _sync.warning(
        "[enc_buf] DROP output=%s total=%d qsize=%d/%d",
        output_name, total_dropped, qsize, qmax,
    )


def log_encoding_buffer_high(output_name: str, qsize: int, qmax: int) -> None:
    """Per-output queue approaching cap (encoder lagging)."""
    _sync.warning(
        "[enc_buf] HIGH output=%s qsize=%d/%d (encoder falling behind)",
        output_name, qsize, qmax,
    )


def log_encoding_buffer_recovered(output_name: str, qsize: int, qmax: int) -> None:
    """Per-output queue back below the HIGH threshold."""
    _sync.info(
        "[enc_buf] RECOVERED output=%s qsize=%d/%d",
        output_name, qsize, qmax,
    )
