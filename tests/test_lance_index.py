"""Tests for LanceIndex — LanceDB columnar photo index."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ouestcharlie_toolkit.backends.local import LocalBackend
from ouestcharlie_toolkit.lance_index import (
    PHOTO_TABLE_NAME,
    LanceIndex,
    _esc,
    photo_entry_to_row,
    row_to_photo_entry,
)
from ouestcharlie_toolkit.schema import PhotoEntry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry(
    filename: str = "test.jpg",
    content_hash: str = "hash_test",
    searchable: dict | None = None,
) -> PhotoEntry:
    return PhotoEntry(filename=filename, content_hash=content_hash, searchable=searchable or {})


# ---------------------------------------------------------------------------
# _esc — SQL injection protection
# ---------------------------------------------------------------------------


def test_esc_clean_string_unchanged():
    assert _esc("hello") == "hello"


def test_esc_single_quote_doubled():
    assert _esc("it's") == "it''s"


def test_esc_multiple_quotes():
    assert _esc("a'b'c") == "a''b''c"


def test_esc_empty_string():
    assert _esc("") == ""


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


def test_row_tags_defaults_to_empty_list():
    row = photo_entry_to_row(_entry(), "p", None)
    assert row["tags"] == []


def test_row_tags_populated():
    row = photo_entry_to_row(_entry(searchable={"tags": ["a", "b"]}), "p", None)
    assert row["tags"] == ["a", "b"]


def test_row_last_update_is_not_naive():
    row = photo_entry_to_row(_entry(), "p", None)
    assert row["_last_update"].tzinfo is UTC


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


def test_round_trip_tags_always_a_list():
    """Tags field is always a list even when empty."""
    entry = _entry()
    restored = row_to_photo_entry(photo_entry_to_row(entry, "p", None))
    assert isinstance(restored.searchable["tags"], list)


# ---------------------------------------------------------------------------
# LanceIndex.open_or_create / open
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_or_create_creates_index(tmp_path: Path):
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    assert idx is not None


@pytest.mark.asyncio
async def test_open_or_create_is_idempotent(tmp_path: Path):
    backend = LocalBackend(root=tmp_path)
    await LanceIndex.open_or_create(backend, PHOTO_TABLE_NAME)
    idx2 = await LanceIndex.open_or_create(backend, PHOTO_TABLE_NAME)
    assert idx2 is not None


@pytest.mark.asyncio
async def test_open_raises_when_absent(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        await LanceIndex.open(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)


@pytest.mark.asyncio
async def test_open_succeeds_after_create(tmp_path: Path):
    backend = LocalBackend(root=tmp_path)
    await LanceIndex.open_or_create(backend, PHOTO_TABLE_NAME)
    idx = await LanceIndex.open(backend, PHOTO_TABLE_NAME)
    assert idx is not None


# ---------------------------------------------------------------------------
# upsert_partition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_empty_entries_is_no_op(tmp_path: Path):
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    await idx.upsert_partition("p", [], None)  # must not raise
    assert await idx.get_partition_rows("p") == []


@pytest.mark.asyncio
async def test_upsert_inserts_rows(tmp_path: Path):
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    await idx.upsert_partition("part", [_entry("a.jpg", "hash_a"), _entry("b.jpg", "hash_b")], None)
    rows = await idx.get_partition_rows("part")
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_upsert_overwrites_matching_hash(tmp_path: Path):
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    await idx.upsert_partition("part", [_entry("a.jpg", "hash_a", {"rating": 3})], None)
    await idx.upsert_partition("part", [_entry("a.jpg", "hash_a", {"rating": 5})], None)
    rows = await idx.get_partition_rows("part")
    assert len(rows) == 1
    assert rows[0]["rating"] == 5


@pytest.mark.asyncio
async def test_upsert_preserves_existing_thumbnail(tmp_path: Path):
    """Re-upserting without thumbnail_lookup must not wipe thumbnail data."""
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    e = _entry("a.jpg", "hash_a")
    await idx.upsert_partition("part", [e], {"hash_a": ("avif1", 2)})
    await idx.upsert_partition("part", [e], None)  # no lookup → preserve
    rows = await idx.get_partition_rows("part")
    assert rows[0]["thumbnail_avif_hash"] == "avif1"
    assert int(rows[0]["thumbnail_tile_index"]) == 2


@pytest.mark.asyncio
async def test_upsert_thumbnail_lookup_overrides_existing(tmp_path: Path):
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    e = _entry("a.jpg", "hash_a")
    await idx.upsert_partition("part", [e], {"hash_a": ("avif1", 0)})
    await idx.upsert_partition("part", [e], {"hash_a": ("avif2", 7)})
    rows = await idx.get_partition_rows("part")
    assert rows[0]["thumbnail_avif_hash"] == "avif2"
    assert int(rows[0]["thumbnail_tile_index"]) == 7


@pytest.mark.asyncio
async def test_upsert_same_hash_different_partitions_creates_two_rows(tmp_path: Path):
    """The same content_hash in two partitions must produce two independent rows."""
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    await idx.upsert_partition("p1", [_entry("a.jpg", "hash_x")], None)
    await idx.upsert_partition("p2", [_entry("b.jpg", "hash_x")], None)
    assert len(await idx.get_partition_rows("p1")) == 1
    assert len(await idx.get_partition_rows("p2")) == 1
    assert len(await idx.search_where(None)) == 2


@pytest.mark.asyncio
async def test_upsert_duplicate_hash_in_batch_keeps_first(tmp_path: Path):
    """Two entries with the same hash in one batch must produce exactly one row."""
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    entries = [_entry("first.jpg", "hash_dup"), _entry("second.jpg", "hash_dup")]
    await idx.upsert_partition("p", entries, None)
    rows = await idx.get_partition_rows("p")
    assert len(rows) == 1
    assert rows[0]["filename"] == "first.jpg"


# ---------------------------------------------------------------------------
# delete/ delete_partition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_photos_removes_matching(tmp_path: Path):
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    await idx.upsert_partition("p", [_entry("a.jpg", "hash_a"), _entry("b.jpg", "hash_b")], None)
    await idx.delete("p", ["hash_a"])
    rows = await idx.get_partition_rows("p")
    assert len(rows) == 1
    assert rows[0]["content_hash"] == "hash_b"


@pytest.mark.asyncio
async def test_delete_photos_empty_list_is_no_op(tmp_path: Path):
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    await idx.upsert_partition("p", [_entry("a.jpg", "hash_a")], None)
    await idx.delete("p", [])
    assert len(await idx.get_partition_rows("p")) == 1


@pytest.mark.asyncio
async def test_delete_partition_removes_all_rows(tmp_path: Path):
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    await idx.upsert_partition("p1", [_entry("a.jpg", "hash_a"), _entry("b.jpg", "hash_b")], None)
    await idx.upsert_partition("p2", [_entry("c.jpg", "hash_c")], None)
    await idx.delete_partition("p1")
    assert await idx.get_partition_rows("p1") == []
    assert len(await idx.get_partition_rows("p2")) == 1


@pytest.mark.asyncio
async def test_delete_partition_leaves_other_partitions(tmp_path: Path):
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    await idx.upsert_partition("keep", [_entry("a.jpg", "hash_a")], None)
    await idx.upsert_partition("drop", [_entry("b.jpg", "hash_b")], None)
    await idx.delete_partition("drop")
    assert len(await idx.get_partition_rows("keep")) == 1


# ---------------------------------------------------------------------------
# get_partition_rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_partition_rows_filters_by_partition(tmp_path: Path):
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    await idx.upsert_partition("a", [_entry("x.jpg", "hash_x")], None)
    await idx.upsert_partition("b", [_entry("y.jpg", "hash_y")], None)
    rows_a = await idx.get_partition_rows("a")
    assert len(rows_a) == 1
    assert rows_a[0]["filename"] == "x.jpg"


@pytest.mark.asyncio
async def test_get_partition_rows_empty_when_absent(tmp_path: Path):
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    assert await idx.get_partition_rows("nonexistent") == []


# ---------------------------------------------------------------------------
# search_where
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_where_no_filter_returns_all(tmp_path: Path):
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    await idx.upsert_partition("a", [_entry("x.jpg", "hash_x")], None)
    await idx.upsert_partition("b", [_entry("y.jpg", "hash_y")], None)
    assert len(await idx.search_where(None)) == 2


@pytest.mark.asyncio
async def test_search_where_root_limits_to_prefix(tmp_path: Path):
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    await idx.upsert_partition("2024/july", [_entry("a.jpg", "h_a")], None)
    await idx.upsert_partition("2023/march", [_entry("b.jpg", "h_b")], None)
    rows = await idx.search_where(None, root="2024")
    assert len(rows) == 1
    assert rows[0]["filename"] == "a.jpg"


@pytest.mark.asyncio
async def test_search_where_root_includes_exact_match(tmp_path: Path):
    """root matches both the exact partition and partitions with that prefix."""
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    await idx.upsert_partition("2024", [_entry("a.jpg", "h_a")], None)
    await idx.upsert_partition("2024/july", [_entry("b.jpg", "h_b")], None)
    assert len(await idx.search_where(None, root="2024")) == 2


@pytest.mark.asyncio
async def test_search_where_clause_filters_rows(tmp_path: Path):
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    await idx.upsert_partition(
        "p",
        [
            _entry("hi.jpg", "h_hi", {"rating": 5}),
            _entry("lo.jpg", "h_lo", {"rating": 2}),
        ],
        None,
    )
    rows = await idx.search_where("rating >= 4")
    assert len(rows) == 1
    assert rows[0]["filename"] == "hi.jpg"


@pytest.mark.asyncio
async def test_search_where_combined_root_and_clause(tmp_path: Path):
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    await idx.upsert_partition("2024/july", [_entry("a.jpg", "h_a", {"rating": 5})], None)
    await idx.upsert_partition("2024/july", [_entry("b.jpg", "h_b", {"rating": 2})], None)
    await idx.upsert_partition("2023/jan", [_entry("c.jpg", "h_c", {"rating": 5})], None)
    rows = await idx.search_where("rating >= 4", root="2024")
    assert len(rows) == 1
    assert rows[0]["filename"] == "a.jpg"
