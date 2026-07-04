"""Tests for compute_partition_summary
DuckDB-based partition aggregation.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from ouestcharlie_toolkit.backends.local import LocalBackend
from ouestcharlie_toolkit.lance_index import PHOTO_TABLE_NAME, LanceIndex
from ouestcharlie_toolkit.partition_summary import compute_partition_summary
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
    summary = await compute_partition_summary(idx, "p")
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
    summary = await compute_partition_summary(idx, "p")
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
    summary = await compute_partition_summary(idx, "p")
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
    summary = await compute_partition_summary(idx, "p")
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
    summary = await compute_partition_summary(idx, "p")
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
    summary = await compute_partition_summary(idx, "p")
    assert summary.dateTaken is None


@pytest.mark.asyncio
async def test_missing_count_is_correct(tmp_path: Path) -> None:
    """'missing' count is accurate for an int range field."""
    entries = [
        _entry(0, {"rating": 4}),
        _entry(1, {}),
    ]
    idx = await _index(tmp_path, entries)
    summary = await compute_partition_summary(idx, "p")
    assert summary.rating["missing"] == 1
