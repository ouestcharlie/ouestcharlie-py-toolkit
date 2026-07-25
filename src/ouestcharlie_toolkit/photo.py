"""Photo domain object — identity and EXIF extraction for a single photo file."""

from __future__ import annotations

import logging
import re
from datetime import datetime

from .backend import Backend
from .schema import XmpSidecar

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# EXIF helpers (pyexiv2 key parsing)
# ---------------------------------------------------------------------------


def _parse_exif_datetime(exif: dict[str, str]) -> datetime | None:
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


def _exif_rational_to_float(r: str) -> float:
    """Convert EXIF rational string '12345/1000' to float."""
    n, d = r.split("/")
    return int(n) / int(d)


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
    except (ValueError, TypeError):
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


def _map_exif_extra(exif: dict[str, str]) -> dict[str, str]:
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


def _parse_exif_gps(exif: dict[str, str]) -> tuple[float, float] | None:
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
            total = sum(_exif_rational_to_float(p) / (60.0**i) for i, p in enumerate(parts))
            return -total if ref in ("S", "W") else total

        return (dms_to_decimal(lat_raw, lat_ref), dms_to_decimal(lon_raw, lon_ref))
    except (ValueError, ZeroDivisionError, IndexError):
        _log.debug("Could not parse EXIF GPS %r / %r", lat_raw, lon_raw, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Photo class
# ---------------------------------------------------------------------------


class Photo:
    """Represents a single photo file in a backend.

    Provides two operations used together at ingestion:

    - ``create_identity()`` — SHA-256 content hash (stable, format-agnostic ID)
    - ``extract_exif()``    — EXIF metadata extracted into an XmpSidecar

    Both operations read the photo file.  Calling ``extract_exif()`` first
    caches the hash so a subsequent ``create_identity()`` call is free.
    """

    def __init__(self, backend: Backend, path: str) -> None:
        """
        Args:
            backend: Backend that owns the photo file.
            path: Relative path to the photo within the backend root.
        """
        self.backend = backend
        self.path = path
        self._content_hash: str | None = None

    async def create_identity(self) -> str:
        """Return the BLAKE3 content hash of this photo.

        If ``extract_exif()`` was already called, the cached hash is returned
        without re-reading the file.

        Returns:
            22-character base64url string (BLAKE3 truncated to 128 bits).
        """
        if self._content_hash is None:
            self._content_hash = await self.backend.content_hash(self.path)
        return self._content_hash

    async def extract_exif(self) -> XmpSidecar:
        """Extract EXIF metadata from this photo into an XmpSidecar.

        Also caches the content hash so a subsequent ``create_identity()``
        call does not re-read the file.

        Returns:
            XmpSidecar populated with EXIF fields and ``content_hash``.
        """
        import pyexiv2  # lazy: native C extension with system library dependency

        pyexiv2.set_log_level(4)  # mute C-level logs: they write to stdout, corrupting MCP stdio

        # ValueError is raised by content_hash() for empty/dehydrated files.
        photo_hash = await self.backend.content_hash(self.path)

        local = await self.backend.local_path(self.path)
        img = pyexiv2.Image(str(local))  # pyexiv2 requires str, not Path
        try:
            exif_data: dict[str, str] = img.read_exif()
        except UnicodeDecodeError:
            # Legacy cameras (pre-2005 era) often embed EXIF strings in latin-1/cp1252.
            # Latin-1 is lossless for all byte values, so this never raises.
            exif_data = img.read_exif(encoding="latin-1")
        img.close()

        self._content_hash = photo_hash

        date_taken = _parse_exif_datetime(exif_data)
        camera_make = (exif_data.get("Exif.Image.Make") or "").strip() or None
        camera_model = (exif_data.get("Exif.Image.Model") or "").strip() or None
        orientation_s = exif_data.get("Exif.Image.Orientation")
        if isinstance(orientation_s, list):
            orientation_s = orientation_s[0] if orientation_s else None
        orientation = int(orientation_s) if orientation_s else None
        gps = _parse_exif_gps(exif_data)

        def _int_or_none(v: str | None) -> int | None:
            try:
                return int(v) if v else None
            except (ValueError, TypeError):
                return None

        width_s = exif_data.get("Exif.Photo.PixelXDimension") or exif_data.get(
            "Exif.Image.ImageWidth"
        )
        height_s = exif_data.get("Exif.Photo.PixelYDimension") or exif_data.get(
            "Exif.Image.ImageLength"
        )
        width = _int_or_none(width_s)
        height = _int_or_none(height_s)

        def _rational_or_none(v: str | None) -> float | None:
            if not v:
                return None
            try:
                if "/" in v:
                    n, d = v.split("/", 1)
                    dv = int(d)
                    return int(n) / dv if dv else None
                return float(v)
            except (ValueError, TypeError, ZeroDivisionError):
                return None

        # ISO — pyexiv2 returns a single value or space-separated list; take first token
        iso_raw = exif_data.get("Exif.Photo.ISOSpeedRatings")
        iso_speed = _int_or_none(iso_raw.split()[0] if iso_raw else None)

        aperture = _rational_or_none(exif_data.get("Exif.Photo.FNumber"))
        exposure_time = _rational_or_none(exif_data.get("Exif.Photo.ExposureTime"))
        focal_length = _rational_or_none(exif_data.get("Exif.Photo.FocalLength"))
        focal_length_35mm_s = exif_data.get("Exif.Photo.FocalLengthIn35mmFilm")
        focal_length_35mm = _int_or_none(focal_length_35mm_s)
        lens_raw = exif_data.get("Exif.Photo.LensModel") or ""
        lens_model = lens_raw.strip() or None

        # Bootstrap tags from Windows XPKeywords on first extraction: no dc:subject
        # exists yet at this point (extract_exif reads only EXIF, never an existing
        # sidecar), so this never overrides tags edited later via Woof or Darktable.
        # XPKeywords is a null-terminated UTF-16LE byte array; pyexiv2 decodes the
        # UTF-16 but keeps the trailing NUL, so it must be stripped before .strip()
        # (str.strip() only removes whitespace, not the \x00 control character).
        xp_keywords = (exif_data.get("Exif.Image.XPKeywords") or "").rstrip("\x00")
        tags = [t.strip() for t in xp_keywords.split(";") if t.strip()]

        # Bootstrap description from EXIF on first extraction, same rationale as
        # the XPKeywords fallback above: ImageDescription is the cross-platform
        # standard caption field, checked first; XPSubject (Windows Explorer/
        # Photos "Subject") is a Windows-only fallback, same UTF-16LE NUL-
        # termination quirk as XPKeywords.
        description = (exif_data.get("Exif.Image.ImageDescription") or "").strip() or None
        if not description:
            xp_subject = (exif_data.get("Exif.Image.XPSubject") or "").rstrip("\x00")
            description = xp_subject.strip() or None

        return XmpSidecar(
            content_hash=photo_hash,
            date_taken=date_taken,
            camera_make=camera_make,
            camera_model=camera_model,
            orientation=orientation,
            gps=gps,
            width=width,
            height=height,
            iso_speed=iso_speed,
            aperture=aperture,
            exposure_time=exposure_time,
            focal_length=focal_length,
            focal_length_35mm=focal_length_35mm,
            lens_model=lens_model,
            tags=tags,
            description=description,
            _extra=_map_exif_extra(exif_data),
        )
