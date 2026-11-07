"""Recover a capture time from a timestamped filename.

Many camera apps and exports name files with the local wall-clock
(``YYYYMMDD_HHMMSS``, e.g. ``VID_20220830-131551.mp4`` or ``IMG_20240501_120000.jpg``)
while embedded metadata may carry a UTC instant, a different offset, or nothing at
all. These helpers extract the naive local datetime (or date) from such names so
video and photo extraction can share one last-resort fallback.
"""

from __future__ import annotations

import re
from datetime import datetime

# Local wall-clock in a filename, e.g. "20260111_121541" or "20220830-131551".
# Separator between date and time is optional.
_FILENAME_DATETIME = re.compile(r"(\d{8})[_-]?(\d{6})")

# Date only, no time — e.g. WhatsApp "VID-20250317-WA0009" (the WA suffix is a
# message sequence, not a time). The 8 digits must not be immediately followed by 6
# more (that would be a full datetime, handled by ``datetime_from_filename``).
_FILENAME_DATE = re.compile(r"(?<!\d)(\d{8})(?![_-]?\d{6})")


def datetime_from_filename(filename: str | None) -> datetime | None:
    """Parse a naive local datetime from a ``YYYYMMDD_HHMMSS`` filename, or None."""
    if not filename:
        return None
    m = _FILENAME_DATETIME.search(filename)
    if m is None:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def date_from_filename(filename: str | None) -> datetime | None:
    """Parse a date-only ``YYYYMMDD`` filename to midnight local, or None.

    For names that carry a date but no time (e.g. WhatsApp ``VID-20250317-WA…``).
    Time is unrecoverable, so the result lands at 00:00:00 — enough to group on the
    correct calendar day.
    """
    if not filename:
        return None
    m = _FILENAME_DATE.search(filename)
    if m is None:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d")
    except ValueError:
        return None
