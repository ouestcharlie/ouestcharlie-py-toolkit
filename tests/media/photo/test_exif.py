"""Tests for the EXIF parsing helpers."""

import logging

from ouestcharlie_toolkit.media.photo.exif import (
    _decode_undefined_ascii,
    map_exif_extra,
    parse_exif_datetime,
    parse_exif_gps,
)
from tests.format_samples import SAMPLES_DIR

_SAMPLES = SAMPLES_DIR

# Minimal valid JPEG (SOI + JFIF APP0 + EOI) — no EXIF data.
_MINIMAL_JPEG = (
    b"\xff\xd8"  # SOI
    b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"  # APP0
    b"\xff\xd9"  # EOI
)


# ---------------------------------------------------------------------------
# Logging behaviour in EXIF helpers
# ---------------------------------------------------------------------------

_PHOTO_LOGGER = "ouestcharlie_toolkit.media.photo"


def test_parse_exif_datetime_zero_sentinel_returns_none():
    """'0000:00:00 00:00:00' is written by cameras with no RTC; must return None silently."""
    result = parse_exif_datetime({"Exif.Photo.DateTimeOriginal": "0000:00:00 00:00:00"})
    assert result is None


def test_parse_exif_datetime_invalid_logs_debug(caplog):
    """parse_exif_datetime with an unparseable string emits a DEBUG message."""
    bad_exif = {"Exif.Photo.DateTimeOriginal": "not a date"}
    with caplog.at_level(logging.DEBUG, logger=_PHOTO_LOGGER):
        result = parse_exif_datetime(bad_exif)
    assert result is None
    assert any("Could not parse EXIF datetime" in msg for msg in caplog.messages)
    assert any(r.levelno == logging.DEBUG for r in caplog.records)


def test_decode_undefined_ascii_invalid_logs_debug(caplog):
    """_decode_undefined_ascii with non-integer bytes emits a DEBUG message."""
    with caplog.at_level(logging.DEBUG, logger=_PHOTO_LOGGER):
        result = _decode_undefined_ascii("xx yy zz")  # spaces → triggers decode path, then fails
    assert result == "xx yy zz"  # original value returned on failure
    assert any("Could not decode UNDEFINED ASCII" in msg for msg in caplog.messages)


def test_map_exif_extra_hex_local_name_is_prefixed():
    """Unknown EXIF tags with hex IDs (e.g. 0xea1d) are prefixed with 'proprietary_'.

    pyexiv2 uses hex tag IDs for tags it cannot map to a named field.  These
    are not valid XML NCNames (cannot start with a digit), so map_exif_extra
    must rename them to avoid producing unserializable XMP attributes.
    """
    exif = {
        "Exif.Photo.0xea1d": "0",  # unknown exif tag — hex id
        "Exif.Image.0xc6d2": "1 2 3",  # unknown tiff tag — hex id
        "Exif.Photo.Make": "Apple",  # normal key — unchanged
    }
    extra = map_exif_extra(exif)

    exif_ns = "http://ns.adobe.com/exif/1.0/"
    tiff_ns = "http://ns.adobe.com/tiff/1.0/"

    assert f"{{{exif_ns}}}proprietary_0xea1d" in extra
    assert f"{{{tiff_ns}}}proprietary_0xc6d2" in extra
    # Original hex keys must not appear (would produce invalid XML)
    assert f"{{{exif_ns}}}0xea1d" not in extra
    assert f"{{{tiff_ns}}}0xc6d2" not in extra
    # Normal keys are unaffected
    assert f"{{{exif_ns}}}Make" in extra


def test_parse_exif_gps_invalid_logs_debug(caplog):
    """parse_exif_gps with malformed rational strings emits a DEBUG message."""
    bad_exif = {
        "Exif.GPSInfo.GPSLatitudeRef": "N",
        "Exif.GPSInfo.GPSLongitudeRef": "E",
        "Exif.GPSInfo.GPSLatitude": "not/valid",
        "Exif.GPSInfo.GPSLongitude": "2/1",
    }
    with caplog.at_level(logging.DEBUG, logger=_PHOTO_LOGGER):
        result = parse_exif_gps(bad_exif)
    assert result is None
    assert any("Could not parse EXIF GPS" in msg for msg in caplog.messages)
