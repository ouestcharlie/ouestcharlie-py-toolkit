"""Tests for PhotoEntry <-> Lance row conversion."""

from __future__ import annotations

from datetime import UTC, datetime

from ouestcharlie_toolkit.index import (
    LanceIndex,
    photo_entry_to_row,
    row_to_photo_entry,
)
from ouestcharlie_toolkit.model.entries import PhotoEntry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry(
    filename: str = "test.jpg",
    content_hash: str = "hash_test",
    searchable: dict | None = None,
) -> PhotoEntry:
    return PhotoEntry(filename=filename, content_hash=content_hash, searchable=searchable or {})


async def _collect_search(
    idx: LanceIndex,
    where: str | None = None,
    **kwargs,
) -> tuple[list[dict], int]:
    """Collect all rows from search_where into a plain list."""
    matches, total = await idx.search_where(where, **kwargs)
    return matches, total


async def _collect_partition(
    idx: LanceIndex,
    partition: str,
    **kwargs,
) -> list[dict]:
    """Collect all rows from get_partition_rows into a plain list."""
    return [r async for r in idx.get_partition_rows(partition, **kwargs)]


# ---------------------------------------------------------------------------
# photo_entry_to_row
# ---------------------------------------------------------------------------


def test_row_basic_fields():
    entry = _entry("a.jpg", "hash_a")
    row = photo_entry_to_row(entry, "p", None)
    assert row["filename"] == "a.jpg"
    assert row["content_hash"] == "hash_a"
    assert row["partition"] == "p"


def test_row_no_thumbnail():
    row = photo_entry_to_row(_entry(), "p", None)
    assert row["thumbnail_avif_hash"] is None
    assert row["thumbnail_tile_index"] is None


def test_row_with_thumbnail():
    row = photo_entry_to_row(_entry(), "p", ("avifhash", 3))
    assert row["thumbnail_avif_hash"] == "avifhash"
    assert row["thumbnail_tile_index"] == 3


def test_row_gps_present():
    row = photo_entry_to_row(_entry(searchable={"gps": (48.85, 2.35)}), "p", None)
    assert row["gps_lat"] == 48.85
    assert row["gps_lon"] == 2.35


def test_row_gps_absent():
    row = photo_entry_to_row(_entry(), "p", None)
    assert row["gps_lat"] is None
    assert row["gps_lon"] is None


def test_row_date_taken_made_naive():
    """Timezone-aware datetime is stored as naive (LanceDB stores UTC internally)."""
    dt = datetime(2024, 6, 15, 10, 30, tzinfo=UTC)
    row = photo_entry_to_row(_entry(searchable={"date_taken": dt}), "p", None)
    assert row["date_taken"].tzinfo is None
    assert row["date_taken"].year == 2024


def test_row_date_taken_utc_and_offset_from_aware_datetime():
    """A tz-aware date_taken yields local wall-clock, its UTC instant, and the offset."""
    from datetime import timedelta, timezone

    dt = datetime(2020, 7, 15, 20, 0, tzinfo=timezone(timedelta(hours=2)))  # 20:00 +02:00
    row = photo_entry_to_row(_entry(searchable={"date_taken": dt}), "p", None)
    assert row["date_taken"] == datetime(2020, 7, 15, 20, 0)  # naive local wall-clock
    assert row["date_taken_utc"] == datetime(2020, 7, 15, 18, 0)  # UTC instant
    assert row["utc_offset_minutes"] == 120


def test_row_date_taken_utc_null_when_naive():
    """A naive date_taken (no offset) leaves the UTC columns null."""
    entry = _entry(searchable={"date_taken": datetime(2020, 7, 15, 20, 0)})
    row = photo_entry_to_row(entry, "p", None)
    assert row["date_taken"] == datetime(2020, 7, 15, 20, 0)
    assert row["date_taken_utc"] is None
    assert row["utc_offset_minutes"] is None


def test_row_tags_defaults_to_empty_list():
    row = photo_entry_to_row(_entry(), "p", None)
    assert row["tags"] == []


def test_row_tags_populated():
    row = photo_entry_to_row(_entry(searchable={"tags": ["a", "b"]}), "p", None)
    assert row["tags"] == ["a", "b"]


def test_row_tag_terms_derived_from_tag_paths():
    row = photo_entry_to_row(_entry(searchable={"tags": ["Places|Europe", "Family"]}), "p", None)
    # tags keep their spelling; tag_terms are folded for case-insensitive search
    assert row["tags"] == ["Places|Europe", "Family"]
    assert row["tag_terms"] == ["places", "places|europe", "europe", "family"]


def test_row_tag_terms_empty_without_tags():
    assert photo_entry_to_row(_entry(), "p", None)["tag_terms"] == []


def test_row_last_update_is_naive_for_lancedb():
    # LanceDB stores timestamps without timezone; _last_update must be naive (tzinfo=None).
    row = photo_entry_to_row(_entry(), "p", None)
    assert row["_last_update"].tzinfo is None


# ---------------------------------------------------------------------------
# row_to_photo_entry
# ---------------------------------------------------------------------------


def test_round_trip_scalar_fields():
    entry = _entry(
        "img.jpg",
        "hash_img",
        searchable={
            "make": "Sony",
            "model": "A7 IV",
            "rating": 5,
            "width": 7008,
            "height": 4672,
            "orientation": 1,
        },
    )
    row = photo_entry_to_row(entry, "2024/", None)
    restored = row_to_photo_entry(row)
    assert restored.filename == "img.jpg"
    assert restored.content_hash == "hash_img"
    assert restored.searchable["make"] == "Sony"
    assert restored.searchable["rating"] == 5
    assert restored.searchable["width"] == 7008


def test_round_trip_gps():
    entry = _entry(searchable={"gps": (48.85, 2.35)})
    restored = row_to_photo_entry(photo_entry_to_row(entry, "p", None))
    assert restored.searchable["gps"] == (48.85, 2.35)


def test_round_trip_gps_absent():
    entry = _entry()
    restored = row_to_photo_entry(photo_entry_to_row(entry, "p", None))
    assert restored.searchable.get("gps") is None


def test_round_trip_tags():
    entry = _entry(searchable={"tags": ["sunset", "travel"]})
    restored = row_to_photo_entry(photo_entry_to_row(entry, "p", None))
    assert restored.searchable["tags"] == ["sunset", "travel"]


def test_round_trip_hierarchical_tags():
    entry = _entry(searchable={"tags": ["Places|Europe|France", "Family"]})
    restored = row_to_photo_entry(photo_entry_to_row(entry, "p", None))
    assert restored.searchable["tags"] == ["Places|Europe|France", "Family"]


def test_round_trip_tags_always_a_list():
    """Tags field is always a list even when empty."""
    entry = _entry()
    restored = row_to_photo_entry(photo_entry_to_row(entry, "p", None))
    assert isinstance(restored.searchable["tags"], list)
