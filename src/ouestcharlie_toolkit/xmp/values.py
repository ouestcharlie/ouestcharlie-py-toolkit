"""Conversions between XMP property strings and Python values (dates, GPS)."""

from __future__ import annotations

import logging
import re
from datetime import datetime

_log = logging.getLogger(__name__)

# EXIF-style date prefix "YYYY:MM:DD " (darktable/Exiv2 write exif:DateTimeOriginal so).
_EXIF_DATE_PREFIX = re.compile(r"(\d{4}):(\d{2}):(\d{2})[ T]")
# XMP partial dates that fromisoformat rejects: "YYYY" and "YYYY-MM".
_XMP_PARTIAL_DATE = re.compile(r"\d{4}(-\d{2})?")


def parse_iso_datetime(s: str | None) -> datetime | None:
    """Parse an XMP datetime string, preserving subseconds and timezone.

    Accepts ISO 8601, the EXIF style ``YYYY:MM:DD HH:MM:SS[.fraction]`` written
    by darktable, and XMP partial dates (``YYYY``, ``YYYY-MM``, ``YYYY-MM-DD``,
    ``YYYY-MM-DDTHH:MM``), which are rounded to the start of the period.

    Returns a timezone-aware datetime when an offset is present, a naive
    datetime otherwise.  Returns None for empty/invalid input.
    """
    if not s:
        return None
    s = s.strip()
    if m := _EXIF_DATE_PREFIX.match(s):
        s = f"{m[1]}-{m[2]}-{m[3]}T{s[m.end() :]}"
    elif _XMP_PARTIAL_DATE.fullmatch(s):
        s = f"{s}-01-01"[:10]
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        _log.debug("Could not parse XMP datetime %r", s, exc_info=True)
        return None


def xmp_coord_to_decimal(coord: str) -> float:
    """Convert XMP GPS coordinate '48,51.396000N' to decimal degrees."""
    ref = coord[-1]
    deg_min = coord[:-1].split(",")
    degrees = float(deg_min[0])
    minutes = float(deg_min[1]) if len(deg_min) > 1 else 0.0
    decimal = degrees + minutes / 60.0
    if ref in ("S", "W"):
        decimal = -decimal
    return decimal


def decimal_to_xmp_coord(value: float, is_lat: bool) -> str:
    """Convert decimal degrees to XMP GPS format '48,51.396000N'."""
    ref = ("N" if value >= 0 else "S") if is_lat else ("E" if value >= 0 else "W")
    abs_val = abs(value)
    degrees = int(abs_val)
    minutes = (abs_val - degrees) * 60.0
    return f"{degrees},{minutes:.6f}{ref}"


def parse_xmp_gps(lat_str: str | None, lon_str: str | None) -> tuple[float, float] | None:
    """Parse XMP GPS coordinate strings into (lat, lon) decimal degrees."""
    if not lat_str or not lon_str:
        return None
    try:
        return (xmp_coord_to_decimal(lat_str), xmp_coord_to_decimal(lon_str))
    except ValueError, IndexError:
        _log.debug("Could not parse XMP GPS %r / %r", lat_str, lon_str, exc_info=True)
        return None
