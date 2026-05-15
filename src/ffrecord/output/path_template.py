"""Path template renderer.

Supported placeholders:
  {output}             output name (from config)
  {CH}                 channel name (from channel.name)
  {YYYYMMDD}           UTC date
  {YYYYMMDDHH}         UTC date + hour (used as hour-directory)
  {starttime_unix_ms}  segment start time as Unix epoch milliseconds
  {seq}                zero-padded 6-digit sequence number (resets per session)
"""

from __future__ import annotations

import datetime
from pathlib import Path


def render(template: str, *, output_name: str, channel_name: str,
           start_unix_ms: int, seq: int = 0) -> str:
    dt = datetime.datetime.fromtimestamp(start_unix_ms / 1000.0, tz=datetime.timezone.utc)
    result = template.format(
        output=output_name,
        CH=channel_name,
        YYYYMMDD=dt.strftime("%Y%m%d"),
        YYYYMMDDHH=dt.strftime("%Y%m%d%H"),
        starttime_unix_ms=start_unix_ms,
        seq=f"{seq:06d}",
    )
    return result


def ensure_parent(path: str) -> Path:
    p = Path(path).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    return p
