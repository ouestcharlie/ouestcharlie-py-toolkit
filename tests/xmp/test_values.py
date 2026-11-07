"""Tests for XMP date and GPS value conversions."""

from datetime import datetime

from ouestcharlie_toolkit.xmp.values import (
    decimal_to_xmp_coord,
    parse_iso_datetime,
    parse_xmp_gps,
    xmp_coord_to_decimal,
)
from tests.format_samples import SAMPLES_DIR

_SAMPLES = SAMPLES_DIR


# ---------------------------------------------------------------------------
# Datetime helpers
# ---------------------------------------------------------------------------


def test_parse_iso_datetime_valid():
    assert parse_iso_datetime("2024-07-15T14:30:00") == datetime(2024, 7, 15, 14, 30, 0)


def test_parse_iso_datetime_preserves_subseconds():
    assert parse_iso_datetime("2024-07-15T14:30:00.123") == datetime(2024, 7, 15, 14, 30, 0, 123000)


def test_parse_iso_datetime_preserves_timezone():
    from datetime import timedelta, timezone

    dt = parse_iso_datetime("2024-07-15T14:30:00.123+01:00")
    assert dt == datetime(2024, 7, 15, 14, 30, 0, 123000, tzinfo=timezone(timedelta(hours=1)))


def test_parse_iso_datetime_none():
    assert parse_iso_datetime(None) is None


def test_parse_iso_datetime_empty():
    assert parse_iso_datetime("") is None


def test_parse_iso_datetime_invalid():
    assert parse_iso_datetime("not-a-date") is None


# ---------------------------------------------------------------------------
# GPS helpers
# ---------------------------------------------------------------------------


def test_xmp_coord_to_decimal_north():
    # 48°30'N = 48.5
    result = xmp_coord_to_decimal("48,30.000000N")
    assert abs(result - 48.5) < 1e-6


def test_xmp_coord_to_decimal_south():
    result = xmp_coord_to_decimal("48,30.000000S")
    assert abs(result - (-48.5)) < 1e-6


def test_xmp_coord_to_decimal_east():
    result = xmp_coord_to_decimal("2,21.132000E")
    assert result > 0


def test_xmp_coord_to_decimal_west():
    result = xmp_coord_to_decimal("2,21.132000W")
    assert result < 0


def test_decimal_to_xmp_coord_lat():
    s = decimal_to_xmp_coord(48.5, is_lat=True)
    assert s.endswith("N")
    assert s.startswith("48,")


def test_decimal_to_xmp_coord_lon_negative():
    s = decimal_to_xmp_coord(-2.352, is_lat=False)
    assert s.endswith("W")


def test_gps_roundtrip():
    """Decimal → XMP coord → decimal should be lossless within float precision."""
    lat, lon = 48.8566, 2.3522
    lat_s = decimal_to_xmp_coord(lat, is_lat=True)
    lon_s = decimal_to_xmp_coord(lon, is_lat=False)
    result = parse_xmp_gps(lat_s, lon_s)
    assert result is not None
    assert abs(result[0] - lat) < 1e-5
    assert abs(result[1] - lon) < 1e-5


def test_parse_xmp_gps_none_when_missing():
    assert parse_xmp_gps(None, "2,21.132000E") is None
    assert parse_xmp_gps("48,30.000000N", None) is None
    assert parse_xmp_gps(None, None) is None
