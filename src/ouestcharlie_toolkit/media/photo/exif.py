"""EXIF parsing: pyexiv2-style tag dicts to dates, GPS and XMP extra fields."""

from __future__ import annotations

import logging
import re
from datetime import datetime

_log = logging.getLogger(__name__)


def parse_exif_datetime(exif: dict[str, str]) -> datetime | None:
    """Parse a timezone-aware datetime from EXIF DateTimeOriginal, SubSec, and OffsetTime.

    Combines three EXIF fields into a single datetime:
    - ``DateTimeOriginal``  / ``DateTime``       — base date and time
    - ``SubSecTimeOriginal``/ ``SubSecTime``      — fractional seconds (optional)
    - ``OffsetTimeOriginal``/ ``OffsetTime``      — UTC offset, e.g. "+01:00" (optional)

    Returns a timezone-aware datetime when an offset is present, naive otherwise.
    """
    date_str = exif.get("Exif.Photo.DateTimeOriginal") or exif.get("Exif.Image.DateTime")
    if not date_str:
        return None
    # Sentinel written by cameras with no RTC (clock not set).
    if date_str.strip() == "0000:00:00 00:00:00":
        return None
    try:
        # "2026:02:21 13:03:10" → "2026-02-21T13:03:10"
        iso = date_str.strip().replace(":", "-", 2).replace(" ", "T")
        subsec = (
            exif.get("Exif.Photo.SubSecTimeOriginal") or exif.get("Exif.Photo.SubSecTime") or ""
        ).strip()
        if subsec:
            iso += f".{subsec[:6]}"  # cap at microsecond precision
        tz = (
            exif.get("Exif.Photo.OffsetTimeOriginal") or exif.get("Exif.Photo.OffsetTime") or ""
        ).strip()
        if tz:
            iso += tz
        return datetime.fromisoformat(iso)
    except ValueError:
        _log.debug("Could not parse EXIF datetime %r", date_str, exc_info=True)
        return None


def exif_rational_to_float(r: str) -> float:
    """Convert EXIF rational string '12345/1000' to float. Also accepts plain integers."""
    n, _, d = r.partition("/")
    return int(n) / int(d) if d else float(n)


# ---------------------------------------------------------------------------
# EXIF → XMP _extra mapping
# ---------------------------------------------------------------------------

# Maps pyexiv2 key prefixes to their XMP namespace URIs.
_EXIF_TO_XMP_NS: dict[str, str] = {
    "Exif.Image.": "http://ns.adobe.com/tiff/1.0/",
    "Exif.Photo.": "http://ns.adobe.com/exif/1.0/",
}

# pyexiv2 represents unknown tags as hex IDs (e.g. "0xea1d").  These are not
# valid XML NCNames (cannot start with a digit), so we prefix them.
_HEX_LOCAL_RE = re.compile(r"^0x[0-9a-fA-F]+$")

# UNDEFINED-type EXIF fields that store ASCII strings as space-separated decimal bytes
# (e.g. "48 50 50 48" → "0220").  pyexiv2 does not decode these automatically.
_EXIF_UNDEFINED_ASCII: frozenset[str] = frozenset(
    {
        "Exif.Photo.ExifVersion",
        "Exif.Photo.FlashpixVersion",
    }
)


def _decode_undefined_ascii(val: str) -> str:
    """Convert pyexiv2's decimal-byte representation of an UNDEFINED ASCII field to a string.

    Some pyexiv2 builds return UNDEFINED data as space-separated decimal bytes
    (e.g. ``"48 50 50 48"`` for ExifVersion "0220"); others return the already-decoded
    ASCII string directly.  Decode only when spaces are present.
    """
    if " " not in val:
        return val  # already a string
    try:
        return "".join(chr(int(b)) for b in val.split())
    except ValueError, TypeError:
        _log.debug("Could not decode UNDEFINED ASCII EXIF field %r", val, exc_info=True)
        return val


# Keys consumed by typed fields, internal JPEG structure, or binary blobs.
_EXIF_EXTRA_SKIP: frozenset[str] = frozenset(
    {
        # Typed fields
        "Exif.Image.Make",
        "Exif.Image.Model",
        "Exif.Image.Orientation",
        "Exif.Photo.DateTimeOriginal",
        "Exif.Image.DateTime",
        "Exif.Photo.SubSecTimeOriginal",
        "Exif.Photo.SubSecTime",
        "Exif.Photo.SubSecTimeDigitized",
        "Exif.Photo.OffsetTimeOriginal",
        "Exif.Photo.OffsetTime",
        "Exif.Photo.OffsetTimeDigitized",
        "Exif.Photo.PixelXDimension",
        "Exif.Photo.PixelYDimension",
        "Exif.Image.ImageWidth",
        "Exif.Image.ImageLength",
        # Internal JPEG / IFD pointers
        "Exif.Image.JPEGInterchangeFormat",
        "Exif.Image.JPEGInterchangeFormatLength",
        "Exif.Image.ExifTag",
        "Exif.Image.GPSTag",
        # Binary blobs
        "Exif.Photo.MakerNote",
        "Exif.Photo.UserComment",
        # Typed shoot-settings fields extracted below
        "Exif.Photo.ISOSpeedRatings",
        "Exif.Photo.FNumber",
        "Exif.Photo.ExposureTime",
        "Exif.Photo.FocalLength",
        "Exif.Photo.FocalLengthIn35mmFilm",
        "Exif.Photo.LensModel",
        # Bootstrapped into tags below
        "Exif.Image.XPKeywords",
        # Bootstrapped into description below
        "Exif.Image.ImageDescription",
        "Exif.Image.XPSubject",
    }
)


def map_exif_extra(exif: dict[str, str]) -> dict[str, str]:
    """Map remaining EXIF fields to XMP Clark-notation keys for _extra.

    Fields already modelled as typed XmpSidecar attributes, GPS coordinates,
    internal JPEG structure pointers, and binary blobs are skipped.
    """
    extra: dict[str, str] = {}
    for key, val in exif.items():
        if key in _EXIF_EXTRA_SKIP or key.startswith("Exif.GPSInfo."):
            continue
        if key in _EXIF_UNDEFINED_ASCII:
            val = _decode_undefined_ascii(val)
        for prefix, ns_uri in _EXIF_TO_XMP_NS.items():
            if key.startswith(prefix):
                local = key[len(prefix) :]
                if _HEX_LOCAL_RE.match(local):
                    local = f"proprietary_{local}"
                extra[f"{{{ns_uri}}}{local}"] = val
                break
    return extra


def parse_exif_gps(exif: dict[str, str]) -> tuple[float, float] | None:
    """Extract GPS from a pyexiv2 EXIF dict as (lat, lon) decimal degrees."""
    lat_ref = exif.get("Exif.GPSInfo.GPSLatitudeRef", "")
    lon_ref = exif.get("Exif.GPSInfo.GPSLongitudeRef", "")
    lat_raw = exif.get("Exif.GPSInfo.GPSLatitude")
    lon_raw = exif.get("Exif.GPSInfo.GPSLongitude")
    if not (lat_ref and lon_ref and lat_raw and lon_raw):
        return None
    try:

        def dms_to_decimal(dms: str, ref: str) -> float:
            parts = dms.split()
            total = sum(exif_rational_to_float(p) / (60.0**i) for i, p in enumerate(parts))
            return -total if ref in ("S", "W") else total

        return (dms_to_decimal(lat_raw, lat_ref), dms_to_decimal(lon_raw, lon_ref))
    except ValueError, ZeroDivisionError, IndexError:
        _log.debug("Could not parse EXIF GPS %r / %r", lat_raw, lon_raw, exc_info=True)
        return None
