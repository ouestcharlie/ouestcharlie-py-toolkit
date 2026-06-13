"""Tests for LanceIndex — LanceDB columnar photo index."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import lancedb
import pyarrow as pa
import pytest

from ouestcharlie_toolkit.backends.local import LocalBackend
from ouestcharlie_toolkit.lance_index import (
    PAGE_SIZE,
    PHOTO_SCHEMA,
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


async def _collect_search(
    idx: LanceIndex,
    where: str | None = None,
    partitions: list[str] | None = None,
    **kwargs,
) -> tuple[list[dict], int]:
    """Collect all rows from search_where into a plain list."""
    rows_iter, total, _facets = await idx.search_where(where, partitions, **kwargs)
    return [r async for r in rows_iter], total


async def _collect_partition(
    idx: LanceIndex,
    partition: str,
    **kwargs,
) -> list[dict]:
    """Collect all rows from get_partition_rows into a plain list."""
    return [r async for r in idx.get_partition_rows(partition, **kwargs)]


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


@pytest.mark.asyncio
async def test_open_or_create_new_table_has_all_schema_columns(tmp_path: Path):
    """A brand-new table must contain every column defined in PHOTO_SCHEMA."""
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    schema = await idx._table.schema()
    existing_cols = set(schema.names)
    expected_cols = {field.name for field in PHOTO_SCHEMA}
    assert expected_cols.issubset(existing_cols), (
        f"Missing columns: {expected_cols - existing_cols}"
    )


@pytest.mark.asyncio
async def test_open_or_create_migrates_missing_columns(tmp_path: Path):
    """An existing table with fewer columns gets missing columns added by migration."""
    # Create a minimal table that lacks the new shoot-settings columns.
    minimal_schema = pa.schema(
        [
            pa.field("content_hash", pa.string()),
            pa.field("filename", pa.string()),
            pa.field("partition", pa.string()),
            pa.field("metadata_version", pa.int64()),
            pa.field("xmp_version_token", pa.string()),
            pa.field("_last_update", pa.timestamp("us")),
        ]
    )
    uri = str(tmp_path / ".ouestcharlie" / "index.lance")
    db = await lancedb.connect_async(uri)
    await db.create_table(PHOTO_TABLE_NAME, schema=minimal_schema)

    # open_or_create should detect the existing table and migrate it.
    backend = LocalBackend(root=tmp_path)
    idx = await LanceIndex.open_or_create(backend, PHOTO_TABLE_NAME)
    schema = await idx._table.schema()
    existing_cols = set(schema.names)

    new_cols = {
        "description",
        "iso_speed",
        "aperture",
        "exposure_time",
        "focal_length",
        "focal_length_35mm",
        "lens_model",
    }
    missing = new_cols - existing_cols
    assert not missing, f"Migration did not add columns: {missing}"


@pytest.mark.asyncio
async def test_open_or_create_creates_fts_index_on_description(tmp_path: Path):
    """open_or_create must create an FTS index on the description column."""
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    indices = await idx._table.list_indices()
    fts_on_description = any(
        getattr(i, "columns", None) == ["description"]
        or (hasattr(i, "name") and "description" in getattr(i, "name", ""))
        for i in indices
    )
    assert fts_on_description, f"No FTS index on 'description' found. Indices: {indices}"


# ---------------------------------------------------------------------------
# upsert_partition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_empty_entries_is_no_op(tmp_path: Path):
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    await idx.upsert_partition("p", [], None)  # must not raise
    assert await _collect_partition(idx, "p") == []


@pytest.mark.asyncio
async def test_upsert_inserts_rows(tmp_path: Path):
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    await idx.upsert_partition("part", [_entry("a.jpg", "hash_a"), _entry("b.jpg", "hash_b")], None)
    rows = await _collect_partition(idx, "part")
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_upsert_overwrites_matching_hash(tmp_path: Path):
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    await idx.upsert_partition("part", [_entry("a.jpg", "hash_a", {"rating": 3})], None)
    await idx.upsert_partition("part", [_entry("a.jpg", "hash_a", {"rating": 5})], None)
    rows = await _collect_partition(idx, "part")
    assert len(rows) == 1
    assert rows[0]["rating"] == 5


@pytest.mark.asyncio
async def test_upsert_preserves_existing_thumbnail(tmp_path: Path):
    """Re-upserting without thumbnail_lookup must not wipe thumbnail data."""
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    e = _entry("a.jpg", "hash_a")
    await idx.upsert_partition("part", [e], {"hash_a": ("avif1", 2)})
    await idx.upsert_partition("part", [e], None)  # no lookup → preserve
    rows = await _collect_partition(idx, "part")
    assert rows[0]["thumbnail_avif_hash"] == "avif1"
    assert int(rows[0]["thumbnail_tile_index"]) == 2


@pytest.mark.asyncio
async def test_upsert_thumbnail_lookup_overrides_existing(tmp_path: Path):
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    e = _entry("a.jpg", "hash_a")
    await idx.upsert_partition("part", [e], {"hash_a": ("avif1", 0)})
    await idx.upsert_partition("part", [e], {"hash_a": ("avif2", 7)})
    rows = await _collect_partition(idx, "part")
    assert rows[0]["thumbnail_avif_hash"] == "avif2"
    assert int(rows[0]["thumbnail_tile_index"]) == 7


@pytest.mark.asyncio
async def test_upsert_same_hash_different_partitions_creates_two_rows(tmp_path: Path):
    """The same content_hash in two partitions must produce two independent rows."""
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    await idx.upsert_partition("p1", [_entry("a.jpg", "hash_x")], None)
    await idx.upsert_partition("p2", [_entry("b.jpg", "hash_x")], None)
    assert len(await _collect_partition(idx, "p1")) == 1
    assert len(await _collect_partition(idx, "p2")) == 1
    rows, total = await _collect_search(idx, None)
    assert len(rows) == 2
    assert total == 2


@pytest.mark.asyncio
async def test_upsert_duplicate_hash_in_batch_keeps_first(tmp_path: Path):
    """Two entries with the same hash in one batch must produce exactly one row."""
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    entries = [_entry("first.jpg", "hash_dup"), _entry("second.jpg", "hash_dup")]
    await idx.upsert_partition("p", entries, None)
    rows = await _collect_partition(idx, "p")
    assert len(rows) == 1
    assert rows[0]["filename"] == "first.jpg"


# ---------------------------------------------------------------------------
# delete / delete_partition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_photos_removes_matching(tmp_path: Path):
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    await idx.upsert_partition("p", [_entry("a.jpg", "hash_a"), _entry("b.jpg", "hash_b")], None)
    await idx.delete("p", ["hash_a"])
    rows = await _collect_partition(idx, "p")
    assert len(rows) == 1
    assert rows[0]["content_hash"] == "hash_b"


@pytest.mark.asyncio
async def test_delete_photos_empty_list_is_no_op(tmp_path: Path):
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    await idx.upsert_partition("p", [_entry("a.jpg", "hash_a")], None)
    await idx.delete("p", [])
    assert len(await _collect_partition(idx, "p")) == 1


@pytest.mark.asyncio
async def test_delete_partition_removes_all_rows(tmp_path: Path):
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    await idx.upsert_partition("p1", [_entry("a.jpg", "hash_a"), _entry("b.jpg", "hash_b")], None)
    await idx.upsert_partition("p2", [_entry("c.jpg", "hash_c")], None)
    await idx.delete_partition("p1")
    assert await _collect_partition(idx, "p1") == []
    assert len(await _collect_partition(idx, "p2")) == 1


@pytest.mark.asyncio
async def test_delete_partition_leaves_other_partitions(tmp_path: Path):
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    await idx.upsert_partition("keep", [_entry("a.jpg", "hash_a")], None)
    await idx.upsert_partition("drop", [_entry("b.jpg", "hash_b")], None)
    await idx.delete_partition("drop")
    assert len(await _collect_partition(idx, "keep")) == 1


# ---------------------------------------------------------------------------
# get_partition_rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_partition_rows_filters_by_partition(tmp_path: Path):
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    await idx.upsert_partition("a", [_entry("x.jpg", "hash_x")], None)
    await idx.upsert_partition("b", [_entry("y.jpg", "hash_y")], None)
    rows_a = await _collect_partition(idx, "a")
    assert len(rows_a) == 1
    assert rows_a[0]["filename"] == "x.jpg"


@pytest.mark.asyncio
async def test_get_partition_rows_empty_when_absent(tmp_path: Path):
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    assert await _collect_partition(idx, "nonexistent") == []


# ---------------------------------------------------------------------------
# search_where
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_where_no_filter_returns_all(tmp_path: Path):
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    await idx.upsert_partition("a", [_entry("x.jpg", "hash_x")], None)
    await idx.upsert_partition("b", [_entry("y.jpg", "hash_y")], None)
    rows, total = await _collect_search(idx, None)
    assert len(rows) == 2
    assert total == 2


@pytest.mark.asyncio
async def test_search_where_root_limits_to_prefix(tmp_path: Path):
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    await idx.upsert_partition("2024/july", [_entry("a.jpg", "h_a")], None)
    await idx.upsert_partition("2023/march", [_entry("b.jpg", "h_b")], None)
    rows, total = await _collect_search(idx, None, partitions=["2024/july"])
    assert len(rows) == 1
    assert total == 1
    assert rows[0]["filename"] == "a.jpg"


@pytest.mark.asyncio
async def test_search_where_root_includes_exact_match(tmp_path: Path):
    """Explicit partitions list supports exact multi-partition selection."""
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    await idx.upsert_partition("2024", [_entry("a.jpg", "h_a")], None)
    await idx.upsert_partition("2024/july", [_entry("b.jpg", "h_b")], None)
    rows, total = await _collect_search(idx, None, partitions=["2024", "2024/july"])
    assert len(rows) == 2
    assert total == 2


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
    rows, total = await _collect_search(idx, "rating >= 4")
    assert len(rows) == 1
    assert total == 1
    assert rows[0]["filename"] == "hi.jpg"


@pytest.mark.asyncio
async def test_search_where_combined_root_and_clause(tmp_path: Path):
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    await idx.upsert_partition("2024/july", [_entry("a.jpg", "h_a", {"rating": 5})], None)
    await idx.upsert_partition("2024/july", [_entry("b.jpg", "h_b", {"rating": 2})], None)
    await idx.upsert_partition("2023/jan", [_entry("c.jpg", "h_c", {"rating": 5})], None)
    rows, total = await _collect_search(idx, "rating >= 4", partitions=["2024/july"])
    assert len(rows) == 1
    assert total == 1
    assert rows[0]["filename"] == "a.jpg"


# ---------------------------------------------------------------------------
# search_where — pagination
# ---------------------------------------------------------------------------


async def _insert_n(idx: LanceIndex, n: int, partition: str = "p") -> None:
    """Insert n photos with sequential filenames and content hashes."""
    entries = [_entry(f"photo_{i:04d}.jpg", f"hash_{i:04d}") for i in range(n)]
    await idx.upsert_partition(partition, entries, None)


@pytest.mark.asyncio
async def test_search_where_total_count_matches_all_rows(tmp_path: Path):
    """total_count reflects the full result set, not just the page."""
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    n = PAGE_SIZE + 10
    await _insert_n(idx, n)
    rows, total = await _collect_search(idx, None, page_size=PAGE_SIZE)
    assert total == n
    assert len(rows) == PAGE_SIZE  # only one page returned


@pytest.mark.asyncio
async def test_search_where_page_zero_returns_first_page(tmp_path: Path):
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    await _insert_n(idx, PAGE_SIZE + 5)
    rows, _ = await _collect_search(idx, None, page=0, page_size=PAGE_SIZE)
    assert len(rows) == PAGE_SIZE


@pytest.mark.asyncio
async def test_search_where_last_page_returns_remainder(tmp_path: Path):
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    remainder = 7
    await _insert_n(idx, PAGE_SIZE + remainder)
    rows, total = await _collect_search(idx, None, page=1, page_size=PAGE_SIZE)
    assert total == PAGE_SIZE + remainder
    assert len(rows) == remainder


@pytest.mark.asyncio
async def test_search_where_page_beyond_total_returns_empty(tmp_path: Path):
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    await _insert_n(idx, 3)
    rows, total = await _collect_search(idx, None, page=1, page_size=PAGE_SIZE)
    assert total == 3
    assert rows == []


@pytest.mark.asyncio
async def test_search_where_pages_do_not_overlap(tmp_path: Path):
    """Rows returned by consecutive pages must be disjoint."""
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    page_size = 3
    await _insert_n(idx, 7)
    rows_p0, _ = await _collect_search(idx, None, page=0, page_size=page_size)
    rows_p1, _ = await _collect_search(idx, None, page=1, page_size=page_size)
    hashes_p0 = {r["content_hash"] for r in rows_p0}
    hashes_p1 = {r["content_hash"] for r in rows_p1}
    assert hashes_p0.isdisjoint(hashes_p1)


@pytest.mark.asyncio
async def test_search_where_pages_cover_all_rows(tmp_path: Path):
    """All pages together must cover every row exactly once."""
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    n = 11
    page_size = 4
    await _insert_n(idx, n)
    all_hashes: set[str] = set()
    for page in range(3):  # pages 0, 1, 2 cover 4+4+3 = 11
        rows, _ = await _collect_search(idx, None, page=page, page_size=page_size)
        for r in rows:
            all_hashes.add(r["content_hash"])
    assert len(all_hashes) == n


@pytest.mark.asyncio
async def test_search_where_total_count_stable_across_pages(tmp_path: Path):
    """total_count must be identical regardless of which page is requested."""
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    n = PAGE_SIZE + 3
    await _insert_n(idx, n)
    _, total_p0 = await _collect_search(idx, None, page=0, page_size=PAGE_SIZE)
    _, total_p1 = await _collect_search(idx, None, page=1, page_size=PAGE_SIZE)
    assert total_p0 == total_p1 == n


@pytest.mark.asyncio
async def test_search_where_filter_and_pagination(tmp_path: Path):
    """WHERE filter is applied before pagination; total_count reflects filtered rows."""
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    # Insert 6 high-rated and 4 low-rated photos
    high = [_entry(f"hi_{i}.jpg", f"hi_{i}", {"rating": 5}) for i in range(6)]
    low = [_entry(f"lo_{i}.jpg", f"lo_{i}", {"rating": 1}) for i in range(4)]
    await idx.upsert_partition("p", high + low, None)
    rows, total = await _collect_search(idx, "rating >= 4", page=0, page_size=4)
    assert total == 6  # only 6 match the filter
    assert len(rows) == 4  # first page of 4
    rows_p1, total_p1 = await _collect_search(idx, "rating >= 4", page=1, page_size=4)
    assert total_p1 == 6
    assert len(rows_p1) == 2  # remaining 2


@pytest.mark.asyncio
async def test_search_where_single_row_fits_in_one_page(tmp_path: Path):
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    await idx.upsert_partition("p", [_entry("only.jpg", "hash_only")], None)
    rows, total = await _collect_search(idx, None, page=0, page_size=PAGE_SIZE)
    assert total == 1
    assert len(rows) == 1
    assert rows[0]["filename"] == "only.jpg"


# ---------------------------------------------------------------------------
# search_where — sort
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_where_results_sorted_descending(tmp_path: Path):
    """Page 0 must return rows ordered newest date_taken first."""
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    old = _entry("old.jpg", "hash_old", {"date_taken": datetime(2022, 1, 1, tzinfo=UTC)})
    mid = _entry("mid.jpg", "hash_mid", {"date_taken": datetime(2023, 6, 15, tzinfo=UTC)})
    new = _entry("new.jpg", "hash_new", {"date_taken": datetime(2024, 12, 31, tzinfo=UTC)})
    # Insert in arbitrary order to ensure DB insertion order differs from sort order.
    await idx.upsert_partition("p", [old, new, mid], None)
    rows, total = await _collect_search(idx, None, order_by="date_taken", order_desc=True)
    assert total == 3
    assert [r["filename"] for r in rows] == ["new.jpg", "mid.jpg", "old.jpg"]


@pytest.mark.asyncio
async def test_search_where_invalid_order_by_does_not_raise(tmp_path: Path):
    """An unknown order_by column must log a warning and return results unsorted."""
    idx = await LanceIndex.open_or_create(LocalBackend(root=tmp_path), PHOTO_TABLE_NAME)
    await idx.upsert_partition("p", [_entry("a.jpg", "h_a"), _entry("b.jpg", "h_b")], None)
    rows, total = await _collect_search(idx, None, order_by="nonexistent_column")
    # Must not raise; must still return all rows.
    assert total == 2
    assert len(rows) == 2
