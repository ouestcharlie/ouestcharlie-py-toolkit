"""Tests for ManifestStore — root summary I/O and optimistic concurrency."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ouestcharlie_toolkit.backend import VersionConflictError
from ouestcharlie_toolkit.backends.local import LocalBackend
from ouestcharlie_toolkit.manifest import ManifestStore
from ouestcharlie_toolkit.schema import (
    SCHEMA_VERSION,
    ManifestSummary,
    RootSummary,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def backend(tmp_path: Path) -> LocalBackend:
    return LocalBackend(root=tmp_path)


@pytest.fixture()
def store(backend: LocalBackend) -> ManifestStore:
    return ManifestStore(backend)


# ---------------------------------------------------------------------------
# RootSummary (summary.json)
# ---------------------------------------------------------------------------


def _summary_with(partitions: list[ManifestSummary] | None = None) -> RootSummary:
    return RootSummary(
        schema_version=SCHEMA_VERSION,
        partitions=partitions or [ManifestSummary(path="2024/2024-07", photo_count=10)],
    )


@pytest.mark.asyncio
async def test_create_summary_writes_file(store: ManifestStore, tmp_path: Path) -> None:
    await store.create_summary(_summary_with())
    expected = tmp_path / ".ouestcharlie" / "summary.json"
    assert expected.exists()
    raw = json.loads(expected.read_text())
    assert raw["schemaVersion"] == SCHEMA_VERSION
    assert len(raw["partitions"]) == 1


@pytest.mark.asyncio
async def test_create_summary_raises_if_exists(store: ManifestStore) -> None:
    await store.create_summary(_summary_with())
    with pytest.raises(FileExistsError):
        await store.create_summary(_summary_with())


@pytest.mark.asyncio
async def test_read_summary_raises_if_missing(store: ManifestStore) -> None:
    with pytest.raises(FileNotFoundError):
        await store.read_summary()


@pytest.mark.asyncio
async def test_read_summary_roundtrip(store: ManifestStore) -> None:
    original = _summary_with(
        [
            ManifestSummary(path="2024/2024-07", photo_count=100),
            ManifestSummary(path="2024/2024-08", photo_count=80),
        ]
    )
    await store.create_summary(original)
    result, _ = await store.read_summary()
    assert result.schema_version == SCHEMA_VERSION
    assert len(result.partitions) == 2
    assert result.partitions[0].path == "2024/2024-07"
    assert result.partitions[0].photo_count == 100


@pytest.mark.asyncio
async def test_write_summary_conflict_raises(store: ManifestStore) -> None:
    summary = _summary_with()
    version = await store.create_summary(summary)
    # Force some delay
    await asyncio.sleep(0.001)
    await store.write_summary(summary, version)
    with pytest.raises(VersionConflictError):
        await store.write_summary(summary, version)


@pytest.mark.asyncio
async def test_upsert_partition_creates_summary(store: ManifestStore, tmp_path: Path) -> None:
    p = ManifestSummary(path="2024/2024-07", photo_count=42)
    result = await store.upsert_partition_in_summary(p)
    assert len(result.partitions) == 1
    assert result.partitions[0].path == "2024/2024-07"
    assert (tmp_path / ".ouestcharlie" / "summary.json").exists()


@pytest.mark.asyncio
async def test_upsert_partition_replaces_existing(store: ManifestStore) -> None:
    await store.create_summary(
        _summary_with([ManifestSummary(path="2024/2024-07", photo_count=10)])
    )
    result = await store.upsert_partition_in_summary(
        ManifestSummary(path="2024/2024-07", photo_count=99)
    )
    assert len(result.partitions) == 1
    assert result.partitions[0].photo_count == 99


@pytest.mark.asyncio
async def test_upsert_partition_appends_new(store: ManifestStore) -> None:
    await store.create_summary(
        _summary_with([ManifestSummary(path="2024/2024-07", photo_count=10)])
    )
    result = await store.upsert_partition_in_summary(
        ManifestSummary(path="2024/2024-08", photo_count=20)
    )
    assert len(result.partitions) == 2


@pytest.mark.asyncio
async def test_upsert_partition_preserves_extra(store: ManifestStore) -> None:
    s = _summary_with()
    s._extra["futureField"] = "keep-me"
    await store.create_summary(s)
    result = await store.upsert_partition_in_summary(
        ManifestSummary(path="2024/2024-08", photo_count=5)
    )
    assert result._extra.get("futureField") == "keep-me"


@pytest.mark.asyncio
async def test_upsert_partition_preserves_other_partitions(
    store: ManifestStore,
) -> None:
    partitions = [
        ManifestSummary(path="2024/2024-07", photo_count=10),
        ManifestSummary(path="2024/2024-08", photo_count=20),
    ]
    await store.create_summary(_summary_with(partitions))
    result = await store.upsert_partition_in_summary(
        ManifestSummary(path="2024/2024-07", photo_count=99)
    )
    other = next(p for p in result.partitions if p.path == "2024/2024-08")
    assert other.photo_count == 20
