"""HEIC/HEIF EXIF reading (pillow-heif + Pillow, bridged to pyexiv2 key format)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

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


def read_heif_exif(path: Path) -> dict[str, str]:
    """Extract EXIF from a HEIC/HEIF file via pillow-heif + Pillow.

    Returns a dict[str, str] in the same pyexiv2-style key format as
    ``pyexiv2.Image.read_exif()``, so downstream parsing (``parse_exif_datetime``,
    ``parse_exif_gps``, ``map_exif_extra``, ...) is format-agnostic.
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
