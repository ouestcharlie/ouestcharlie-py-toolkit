"""Photo domain object — identity and EXIF extraction for a single photo file."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ouestcharlie_toolkit.media.filename_time import date_from_filename, datetime_from_filename
from ouestcharlie_toolkit.media.photo.dimensions import UPRIGHT_DISPLAY_SIZE, raw_size
from ouestcharlie_toolkit.media.photo.exif import (
    exif_rational_to_float,
    map_exif_extra,
    parse_exif_datetime,
    parse_exif_gps,
)
from ouestcharlie_toolkit.media.photo.heif import read_heif_exif
from ouestcharlie_toolkit.media.suffixes import HEIF_SUFFIXES, RAW_SUFFIXES
from ouestcharlie_toolkit.model.entries import XmpSidecar
from ouestcharlie_toolkit.model.tags import normalize_paths
from ouestcharlie_toolkit.storage.backend import Backend


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

    def _read_exif(self, local: Path) -> dict[str, str]:
        """Read the raw EXIF tags of this photo (at *local*), keyed by pyexiv2 tag name."""
        suffix = Path(self.path).suffix.lower()
        if suffix in HEIF_SUFFIXES:
            return read_heif_exif(local)

        import pyexiv2  # lazy: native C extension with system library dependency

        pyexiv2.set_log_level(4)  # mute C-level logs: write to stdout, corrupt MCP stdio

        exif_data: dict[str, str]
        try:
            img = pyexiv2.Image(str(local))  # pyexiv2 requires str, not Path
        except RuntimeError:
            # libexiv2 only reads JPEG XL in its ISO BMFF container. A bare
            # codestream (cjxl's output when there is no metadata) has no EXIF.
            if suffix != ".jxl":
                raise
            exif_data = {}
        else:
            try:
                try:
                    exif_data = img.read_exif()
                except UnicodeDecodeError:
                    # Legacy cameras (pre-2005 era) often embed EXIF strings in
                    # latin-1/cp1252. Latin-1 is lossless for all byte values.
                    exif_data = img.read_exif(encoding="latin-1")
                if suffix in RAW_SUFFIXES and (size := raw_size(img, exif_data)):
                    exif_data["Exif.Photo.PixelXDimension"] = str(size[0])
                    exif_data["Exif.Photo.PixelYDimension"] = str(size[1])
            finally:
                img.close()

        display_size = UPRIGHT_DISPLAY_SIZE.get(suffix)
        if display_size is not None:
            # Upright convention, as for HEIC: the decoder applies the file's own
            # rotation, so store orientation 1 and the display dimensions.
            width, height = display_size(local)
            exif_data["Exif.Image.Orientation"] = "1"
            exif_data["Exif.Photo.PixelXDimension"] = str(width)
            exif_data["Exif.Photo.PixelYDimension"] = str(height)
        return exif_data

    async def exif_datetime(self) -> datetime | None:
        """Return the EXIF capture date-time, timezone-aware when EXIF has an offset.

        Reads only the EXIF metadata, without hashing the file.
        """
        return parse_exif_datetime(self._read_exif(await self.backend.local_path(self.path)))

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
        exif_data = self._read_exif(local)

        self._content_hash = photo_hash

        date_taken = parse_exif_datetime(exif_data)
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
        orientation = int(exif_rational_to_float(orientation_s)) if orientation_s else None
        gps = parse_exif_gps(exif_data)

        def _int_or_none(v: str | None) -> int | None:
            try:
                return int(v) if v else None
            except ValueError, TypeError:
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
        # read_heif_exif; this covers the pyexiv2 path. img.size is the *stored*
        # (pre-rotation) buffer size, so it agrees with the EXIF orientation the
        # same way an EXIF-provided PixelXDimension would — no axis swap (that is
        # the caller's job, per the stored-orientation convention). See HLD
        # "Orientation and stored dimensions".
        if (width is None or height is None) and Path(
            self.path
        ).suffix.lower() not in HEIF_SUFFIXES:
            from PIL import Image

            try:
                with Image.open(local) as pil_img:
                    width, height = pil_img.size
            except OSError, ValueError:
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
            except ValueError, TypeError, ZeroDivisionError:
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
        tags = normalize_paths(xp_keywords.split(";"))

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
            _extra=map_exif_extra(exif_data),
        )
