"""Photo domain object — identity and EXIF extraction for a single photo file."""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path

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
    date_str = (
        exif.get("Exif.Photo.DateTimeOriginal") or exif.get("Exif.Image.DateTime")
    )
    if not date_str:
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

# UNDEFINED-type EXIF fields that store ASCII strings as space-separated decimal bytes
# (e.g. "48 50 50 48" → "0220").  pyexiv2 does not decode these automatically.
_EXIF_UNDEFINED_ASCII: frozenset[str] = frozenset({
    "Exif.Photo.ExifVersion",
    "Exif.Photo.FlashpixVersion",
})


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
_EXIF_EXTRA_SKIP: frozenset[str] = frozenset({
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
    # Internal JPEG / IFD pointers
    "Exif.Image.JPEGInterchangeFormat",
    "Exif.Image.JPEGInterchangeFormatLength",
    "Exif.Image.ExifTag",
    "Exif.Image.GPSTag",
    # Binary blobs
    "Exif.Photo.MakerNote",
    "Exif.Photo.UserComment",
})


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
                local = key[len(prefix):]
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
            total = sum(_exif_rational_to_float(p) / (60.0 ** i) for i, p in enumerate(parts))
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
        """Return the SHA-256 content hash of this photo.

        If ``extract_exif()`` was already called, the cached hash is returned
        without re-reading the file.

        Returns:
            Hash string in the format ``"sha256:<hex>"``.
        """
        if self._content_hash is None:
            data, _ = await self.backend.read(self.path)
            self._content_hash = f"sha256:{hashlib.sha256(data).hexdigest()}"
        return self._content_hash

    async def extract_exif(self) -> XmpSidecar:
        """Extract EXIF metadata from this photo into an XmpSidecar.

        Reads the photo via the backend, writes it to a temporary file, then
        uses pyexiv2 to parse EXIF data. The original image is never modified.

        Also caches the content hash so a subsequent ``create_identity()``
        call does not re-read the file.

        Returns:
            XmpSidecar populated with EXIF fields and ``content_hash``.
        """
        import pyexiv2  # lazy: native C extension with system library dependency
        pyexiv2.set_log_level(4)  # mute C-level logs: they write to stdout, corrupting MCP stdio

        data, _ = await self.backend.read(self.path)
        content_hash = f"sha256:{hashlib.sha256(data).hexdigest()}"
        suffix = Path(self.path).suffix or ".jpg"

        fd, tmp_path = tempfile.mkstemp(suffix=suffix)
        try:
            os.write(fd, data)
            os.close(fd)

            img = pyexiv2.Image(tmp_path)
            exif_data: dict[str, str] = img.read_exif()
            img.close()
        finally:
            os.unlink(tmp_path)

        self._content_hash = content_hash

        date_taken = _parse_exif_datetime(exif_data)
        camera_make = (exif_data.get("Exif.Image.Make") or "").strip() or None
        camera_model = (exif_data.get("Exif.Image.Model") or "").strip() or None
        orientation_s = exif_data.get("Exif.Image.Orientation")
        if isinstance(orientation_s, list):
            orientation_s = orientation_s[0] if orientation_s else None
        orientation = int(orientation_s) if orientation_s else None
        gps = _parse_exif_gps(exif_data)

        return XmpSidecar(
            content_hash=content_hash,
            date_taken=date_taken,
            camera_make=camera_make,
            camera_model=camera_model,
            orientation=orientation,
            gps=gps,
            _extra=_map_exif_extra(exif_data),
        )
