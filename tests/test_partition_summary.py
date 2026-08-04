"""Tests for compute_summary — DuckDB-based aggregation over a Lance index."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from ouestcharlie_toolkit.backends.local import LocalBackend
from ouestcharlie_toolkit.lance_index import PHOTO_TABLE_NAME, FtsFilter, LanceIndex
from ouestcharlie_toolkit.partition_summary import compute_summary
from ouestcharlie_toolkit.schema import PhotoEntry


def _entry(idx: int, searchable: dict | None = None) -> PhotoEntry:
    return PhotoEntry(
        filename=f"photo_{idx}.jpg",
        content_hash=f"hash_{idx:022d}",
        searchable=searchable or {},
    )


async def _index(tmp_path: Path, entries: list[PhotoEntry]) -> LanceIndex:
    idx = await LanceIndex.open(
        LocalBackend(root=tmp_path), PHOTO_TABLE_NAME, create_if_missing=True
    )
    await idx.upsert_partition("p", entries, None)
    return idx


@pytest.mark.asyncio
async def test_no_missing_when_all_have_field(tmp_path: Path) -> None:
    """When every photo has date_taken, no 'missing' key in the stat."""
    entries = [
        _entry(0, {"date_taken": datetime(2024, 1, 1)}),
        _entry(1, {"date_taken": datetime(2024, 6, 1)}),
    ]
    idx = await _index(tmp_path, entries)
    summary = await compute_summary(idx, None)
    assert "missing" not in summary.dateTaken


@pytest.mark.asyncio
async def test_missing_count_for_date_range(tmp_path: Path) -> None:
    """Photos with None date_taken are counted in 'missing'."""
    entries = [
        _entry(0, {"date_taken": datetime(2024, 1, 1)}),
        _entry(1, {"date_taken": None}),
        _entry(2, {}),
    ]
    idx = await _index(tmp_path, entries)
    summary = await compute_summary(idx, None)
    assert summary.dateTaken["missing"] == 2


@pytest.mark.asyncio
async def test_missing_count_for_int_range(tmp_path: Path) -> None:
    """Photos with None rating are counted in 'missing'."""
    entries = [
        _entry(0, {"rating": 3}),
        _entry(1, {"rating": 5}),
        _entry(2, {"rating": None}),
    ]
    idx = await _index(tmp_path, entries)
    summary = await compute_summary(idx, None)
    assert summary.rating["missing"] == 1


@pytest.mark.asyncio
async def test_missing_count_for_gps(tmp_path: Path) -> None:
    """Photos with None GPS are counted in 'missing' for both lat and lon."""
    entries = [
        _entry(0, {"gps": (48.85, 2.35)}),
        _entry(1, {"gps": None}),
        _entry(2, {}),
    ]
    idx = await _index(tmp_path, entries)
    summary = await compute_summary(idx, None)
    assert summary.gps["lat"]["missing"] == 2
    assert summary.gps["lon"]["missing"] == 2


@pytest.mark.asyncio
async def test_gps_missing_counted_per_axis(tmp_path: Path) -> None:
    """lat and lon missing counts are independent when one component is None."""
    entries = [
        _entry(0, {"gps": (48.85, 2.35)}),  # both present
        _entry(1, {"gps": (43.3, None)}),  # lat present, lon missing
        _entry(2, {"gps": (None, 5.37)}),  # lon present, lat missing
        _entry(3, {"gps": None}),  # both missing
    ]
    idx = await _index(tmp_path, entries)
    summary = await compute_summary(idx, None)
    assert summary.gps["lat"]["min"] == 43.3
    assert summary.gps["lat"]["max"] == 48.85
    assert summary.gps["lat"]["missing"] == 2
    assert summary.gps["lon"]["min"] == 2.35
    assert summary.gps["lon"]["max"] == 5.37
    assert summary.gps["lon"]["missing"] == 2


@pytest.mark.asyncio
async def test_no_stat_when_all_missing(tmp_path: Path) -> None:
    """When all photos lack a field, the stat is absent entirely."""
    entries = [_entry(0, {}), _entry(1, {"date_taken": None})]
    idx = await _index(tmp_path, entries)
    summary = await compute_summary(idx, None)
    assert summary.dateTaken is None


@pytest.mark.asyncio
async def test_missing_count_is_correct(tmp_path: Path) -> None:
    """'missing' count is accurate for an int range field."""
    entries = [
        _entry(0, {"rating": 4}),
        _entry(1, {}),
    ]
    idx = await _index(tmp_path, entries)
    summary = await compute_summary(idx, None)
    assert summary.rating["missing"] == 1


# ---------------------------------------------------------------------------
# compute_summary — generalized aggregation (runtime summaries)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_summary_none_scopes_whole_table(tmp_path: Path) -> None:
    """where_clause=None aggregates every row across all partitions."""
    idx = await LanceIndex.open(
        LocalBackend(root=tmp_path), PHOTO_TABLE_NAME, create_if_missing=True
    )
    await idx.upsert_partition("a", [_entry(0, {"rating": 3})], None)
    await idx.upsert_partition("b", [_entry(1, {"rating": 5})], None)

    summary = await compute_summary(idx, None)
    assert summary.photo_count == 2
    assert summary.rating["min"] == 3
    assert summary.rating["max"] == 5


@pytest.mark.asyncio
async def test_compute_summary_filters_rows(tmp_path: Path) -> None:
    """A WHERE clause narrows the aggregate to matching rows."""
    idx = await LanceIndex.open(
        LocalBackend(root=tmp_path), PHOTO_TABLE_NAME, create_if_missing=True
    )
    await idx.upsert_partition("a", [_entry(0, {"rating": 3}), _entry(1, {"rating": 5})], None)

    summary = await compute_summary(idx, "rating >= 5")
    assert summary.photo_count == 1
    assert summary.rating["min"] == 5
    assert summary.rating["max"] == 5


# ---------------------------------------------------------------------------
# tag facets — computed in the same DuckDB pass as the numeric aggregates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_summary_no_filter_counts_all_tags(tmp_path: Path) -> None:
    idx = await LanceIndex.open(
        LocalBackend(root=tmp_path), PHOTO_TABLE_NAME, create_if_missing=True
    )
    await idx.upsert_partition(
        "a",
        [
            _entry(0, {"tags": ["travel", "france"]}),
            _entry(1, {"tags": ["travel"]}),
        ],
        None,
    )
    summary = await compute_summary(idx, None)
    assert summary.tags["counts"] == {"travel": 2, "france": 1}


@pytest.mark.asyncio
async def test_compute_summary_tags_scoped_by_clause(tmp_path: Path) -> None:
    idx = await LanceIndex.open(
        LocalBackend(root=tmp_path), PHOTO_TABLE_NAME, create_if_missing=True
    )
    await idx.upsert_partition("a", [_entry(0, {"tags": ["travel"], "rating": 5})], None)
    await idx.upsert_partition("b", [_entry(1, {"tags": ["work"], "rating": 1})], None)

    summary = await compute_summary(idx, "rating >= 5")
    assert summary.tags["counts"] == {"travel": 1}


@pytest.mark.asyncio
async def test_compute_summary_no_tags_stat_absent(tmp_path: Path) -> None:
    idx = await LanceIndex.open(
        LocalBackend(root=tmp_path), PHOTO_TABLE_NAME, create_if_missing=True
    )
    await idx.upsert_partition("a", [_entry(0)], None)
    summary = await compute_summary(idx, None)
    assert summary.tags is None


# ---------------------------------------------------------------------------
# compute_summary — full-text filter scoping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_summary_fts_filter_scopes_aggregate(tmp_path: Path) -> None:
    """An fts_filter narrows the aggregate to photos matching the FTS query."""
    idx = await LanceIndex.open(
        LocalBackend(root=tmp_path), PHOTO_TABLE_NAME, create_if_missing=True
    )
    await idx.upsert_partition(
        "a",
        [
            _entry(0, {"description": "Red Canyon sunset", "rating": 3}),
            _entry(1, {"description": "Sandy beach waves", "rating": 5}),
        ],
        None,
    )
    summary = await compute_summary(
        idx, None, fts_filter=FtsFilter(query="Canyon", columns=["description"])
    )
    assert summary.photo_count == 1
    assert summary.rating["min"] == 3
    assert summary.rating["max"] == 3


@pytest.mark.asyncio
async def test_compute_summary_fts_filter_combined_with_where(tmp_path: Path) -> None:
    """fts_filter and where_clause both apply: only rows matching both are aggregated."""
    idx = await LanceIndex.open(
        LocalBackend(root=tmp_path), PHOTO_TABLE_NAME, create_if_missing=True
    )
    await idx.upsert_partition(
        "a",
        [
            _entry(0, {"description": "Canyon sunset", "rating": 5, "tags": ["travel"]}),
            _entry(1, {"description": "Canyon sunrise", "rating": 1, "tags": ["work"]}),
            _entry(2, {"description": "Beach waves", "rating": 5, "tags": ["family"]}),
        ],
        None,
    )
    summary = await compute_summary(
        idx, "rating >= 4", fts_filter=FtsFilter(query="Canyon", columns=["description"])
    )
    assert summary.photo_count == 1
    assert summary.tags["counts"] == {"travel": 1}
