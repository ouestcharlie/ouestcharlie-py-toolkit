"""Photo domain object — identity and EXIF extraction for a single photo file."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .backend import Backend
from .filename_time import date_from_filename, datetime_from_filename
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


# ---------------------------------------------------------------------------
# HEIC/HEIF EXIF reading (pillow-heif + Pillow, bridged to pyexiv2 key format)
# ---------------------------------------------------------------------------

# pyexiv2's bundled libexiv2 build lacks libheif support, so HEIC/HEIF files are
# read via pillow-heif + Pillow instead. PIL's Image.getexif() returns a
# dict[int, Any] keyed by numeric TIFF/EXIF tag IDs; this maps the ~20 tags the
# fields above actually consume to their pyexiv2-style key names.
_HEIF_TAG_MAP: dict[int, str] = {
    270: "Exif.Image.ImageDescription",
    271: "Exif.Image.Make",
    272: "Exif.Image.Model",
    274: "Exif.Image.Orientation",
    256: "Exif.Image.ImageWidth",
    257: "Exif.Image.ImageLength",
    36867: "Exif.Photo.DateTimeOriginal",
    36868: "Exif.Photo.DateTimeDigitized",
    306: "Exif.Image.DateTime",
    37521: "Exif.Photo.SubSecTimeOriginal",
    37520: "Exif.Photo.SubSecTime",
    36880: "Exif.Photo.OffsetTime",
    36881: "Exif.Photo.OffsetTimeOriginal",
    40962: "Exif.Photo.PixelXDimension",
    40963: "Exif.Photo.PixelYDimension",
    34855: "Exif.Photo.ISOSpeedRatings",
    33437: "Exif.Photo.FNumber",
    33434: "Exif.Photo.ExposureTime",
    37386: "Exif.Photo.FocalLength",
    41989: "Exif.Photo.FocalLengthIn35mmFilm",
    42036: "Exif.Photo.LensModel",
    40094: "Exif.Image.XPKeywords",
    40095: "Exif.Image.XPSubject",
}

# GPS lives in its own IFD, keyed separately from the tags above.
_HEIF_GPS_TAG_MAP: dict[int, str] = {
    1: "Exif.GPSInfo.GPSLatitudeRef",
    2: "Exif.GPSInfo.GPSLatitude",
    3: "Exif.GPSInfo.GPSLongitudeRef",
    4: "Exif.GPSInfo.GPSLongitude",
    5: "Exif.GPSInfo.GPSAltitudeRef",
    6: "Exif.GPSInfo.GPSAltitude",
}

# XPKeywords/XPSubject are Windows-only UTF-16LE byte arrays; every other tag
# in the maps above is ASCII/numeric/rational.
_HEIF_UTF16_TAGS: frozenset[str] = frozenset({"Exif.Image.XPKeywords", "Exif.Image.XPSubject"})


def _heif_value_to_str(key: str, value: Any) -> str:
    """Convert a PIL EXIF value to the pyexiv2-style string format the rest of this
    module expects: rationals as "n/d", GPS coordinate triples as space-separated
    rationals, and Windows XP byte-array tags decoded from UTF-16LE.
    """
    if key in _HEIF_UTF16_TAGS and isinstance(value, bytes):
        return value.decode("utf-16-le", errors="replace")
    if isinstance(value, list | tuple):
        return " ".join(_heif_value_to_str(key, v) for v in value)
    if hasattr(value, "numerator") and hasattr(value, "denominator"):
        return f"{value.numerator}/{value.denominator}"
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").rstrip("\x00")
    return str(value)


def _read_heif_exif(path: Path) -> dict[str, str]:
    """Extract EXIF from a HEIC/HEIF file via pillow-heif + Pillow.

    Returns a dict[str, str] in the same pyexiv2-style key format as
    ``pyexiv2.Image.read_exif()``, so downstream parsing (``_parse_exif_datetime``,
    ``_parse_exif_gps``, ``_map_exif_extra``, ...) is format-agnostic.
    """
    import pillow_heif  # lazy: native C extension, HEIC files only
    from PIL import Image

    # PIL.TiffImagePlugin logs every EXIF tag at DEBUG on img.getexif(); mute it so
    # HEIC extraction doesn't flood the logs (parallels pyexiv2.set_log_level above).
    logging.getLogger("PIL").setLevel(logging.INFO)

    pillow_heif.register_heif_opener()

    exif_str: dict[str, str] = {}
    with Image.open(path) as img:
        img_width, img_height = img.width, img.height

        # pillow-heif renders HEIC upright and resets the EXIF orientation to 1,
        # exposing the file's real orientation here; libheif (image-proc) likewise
        # applies the HEIF rotation transform when decoding. So the sidecar's
        # orientation stays 1 and its width/height must be the *display* dimensions
        # — img.size is the stored (pre-rotation) size, so swap the axes when the
        # original orientation is a 90°/270° rotation (values 5–8).
        if img.info.get("original_orientation") in (5, 6, 7, 8):
            img_width, img_height = img_height, img_width

        exif = img.getexif()
        if exif:
            tags: dict[int, Any] = dict(exif)
            tags.update(exif.get_ifd(0x8769))  # ExifIFD: DateTimeOriginal, FNumber, ...

            for tag_id, key in _HEIF_TAG_MAP.items():
                value = tags.get(tag_id)
                if value is not None:
                    exif_str[key] = _heif_value_to_str(key, value)

            gps_ifd = exif.get_ifd(0x8825)
            for tag_id, key in _HEIF_GPS_TAG_MAP.items():
                value = gps_ifd.get(tag_id)
                if value is not None:
                    exif_str[key] = _heif_value_to_str(key, value)

        # Dimensions from the decoded image, set last so they always win: many HEIC
        # files carry no PixelXDimension/ImageWidth tags (→ null width/height), and
        # PIL returns SHORT-type tags as raw bytes, so an EXIF PixelXDimension would
        # otherwise overwrite this with a non-numeric string. img.size is the true
        # pixel size regardless.
        exif_str["Exif.Photo.PixelXDimension"] = str(img_width)
        exif_str["Exif.Photo.PixelYDimension"] = str(img_height)

    return exif_str


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


# pyexiv2's bundled libexiv2 lacks libheif support; these suffixes are read
# via pillow-heif + Pillow instead (see _read_heif_exif).
_HEIF_SUFFIXES: frozenset[str] = frozenset({".heic", ".heif", ".hif"})


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
        # ValueError is raised by content_hash() for empty/dehydrated files.
        photo_hash = await self.backend.content_hash(self.path)

        local = await self.backend.local_path(self.path)
        if Path(self.path).suffix.lower() in _HEIF_SUFFIXES:
            exif_data: dict[str, str] = _read_heif_exif(local)
        else:
            import pyexiv2  # lazy: native C extension with system library dependency

            pyexiv2.set_log_level(4)  # mute C-level logs: write to stdout, corrupt MCP stdio

            img = pyexiv2.Image(str(local))  # pyexiv2 requires str, not Path
            try:
                exif_data = img.read_exif()
            except UnicodeDecodeError:
                # Legacy cameras (pre-2005 era) often embed EXIF strings in latin-1/cp1252.
                # Latin-1 is lossless for all byte values, so this never raises.
                exif_data = img.read_exif(encoding="latin-1")
            img.close()

        self._content_hash = photo_hash

        date_taken = _parse_exif_datetime(exif_data)
        if date_taken is None:
            # No usable EXIF timestamp: fall back to a date/time encoded in the
            # filename (e.g. "IMG_20240501_120000.jpg", or a date-only scan/export
            # name). Naive local, offset unknown — same as EXIF without OffsetTime.
            filename = self.path.replace("\\", "/").rsplit("/", 1)[-1]
            date_taken = datetime_from_filename(filename) or date_from_filename(filename)
        camera_make = (exif_data.get("Exif.Image.Make") or "").strip() or None
        camera_model = (exif_data.get("Exif.Image.Model") or "").strip() or None
        orientation_s = exif_data.get("Exif.Image.Orientation")
        if isinstance(orientation_s, list):
            orientation_s = orientation_s[0] if orientation_s else None
        orientation = int(_exif_rational_to_float(orientation_s)) if orientation_s else None
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

        # EXIF carried no dimension tags (common for scans, PNG/WebP, and
        # re-encoded JPEGs that drop PixelXDimension/ImageWidth). The decoded
        # header always has the real pixel size. HEIC already fills these in
        # _read_heif_exif; this covers the pyexiv2 path. img.size is the *stored*
        # (pre-rotation) buffer size, so it agrees with the EXIF orientation the
        # same way an EXIF-provided PixelXDimension would — no axis swap (that is
        # the caller's job, per the stored-orientation convention). See HLD
        # "Orientation and stored dimensions".
        if (width is None or height is None) and Path(
            self.path
        ).suffix.lower() not in _HEIF_SUFFIXES:
            from PIL import Image

            try:
                with Image.open(local) as pil_img:
                    width, height = pil_img.size
            except (OSError, ValueError):
                # Unreadable/unsupported by PIL — leave as null, same as before.
                pass

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
