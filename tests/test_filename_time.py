"""Tests for filename-based capture-time recovery (shared by photo + video)."""

from datetime import datetime

from ouestcharlie_toolkit.filename_time import date_from_filename, datetime_from_filename


def test_datetime_from_filename():
    assert datetime_from_filename("VID_20220701_123033.mp4") == datetime(2022, 7, 1, 12, 30, 33)
    assert datetime_from_filename("IMG_20240501_120000.jpg") == datetime(2024, 5, 1, 12, 0, 0)
    assert datetime_from_filename("20260111121541.mp4") == datetime(2026, 1, 11, 12, 15, 41)
    assert datetime_from_filename("20220830-131551.mov") == datetime(2022, 8, 30, 13, 15, 51)
    assert datetime_from_filename(None) is None
    assert datetime_from_filename("clip.mp4") is None
    assert datetime_from_filename("VID_00000000_000000.mp4") is None  # invalid date


def test_date_from_filename():
    # Date but no time (e.g. WhatsApp: WA0009 is a message sequence, not a clock).
    assert date_from_filename("VID-20250317-WA0009.mp4") == datetime(2025, 3, 17, 0, 0, 0)
    assert date_from_filename("scan-20240501.jpg") == datetime(2024, 5, 1, 0, 0, 0)
    assert date_from_filename(None) is None
    assert date_from_filename("clip.mp4") is None
    assert date_from_filename("20250230.mp4") is None  # invalid calendar date
    # A full datetime name must not be picked up as a bare date here.
    assert date_from_filename("20260111_121541.mp4") is None
