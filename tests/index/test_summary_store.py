"""Tests for SummaryStore — thin root summary I/O and optimistic concurrency."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import pytest

from ouestcharlie_toolkit.index import SummaryStore
from ouestcharlie_toolkit.model.summary import RootSummary
from ouestcharlie_toolkit.model.version import SCHEMA_VERSION
from ouestcharlie_toolkit.storage.backend import PartitionLockToken, VersionConflictError
from ouestcharlie_toolkit.storage.local import LocalBackend

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def backend(tmp_path: Path) -> LocalBackend:
    return LocalBackend(root=tmp_path)


@pytest.fixture()
def store(backend: LocalBackend) -> SummaryStore:
    return SummaryStore(backend)


# ---------------------------------------------------------------------------
# RootSummary (summary.json) — thin marker shape
# ---------------------------------------------------------------------------


def _summary(last_indexed_at: datetime | None = None) -> RootSummary:
    return RootSummary(schema_version=SCHEMA_VERSION, last_indexed_at=last_indexed_at)


@pytest.mark.asyncio
async def test_create_summary_writes_file(store: SummaryStore, tmp_path: Path) -> None:
    await store.create_summary(_summary())
    expected = tmp_path / ".ouestcharlie" / "summary.json"
    assert expected.exists()
    raw = json.loads(expected.read_text())
    assert raw["schemaVersion"] == SCHEMA_VERSION
    assert "partitions" not in raw


@pytest.mark.asyncio
async def test_create_summary_raises_if_exists(store: SummaryStore) -> None:
    await store.create_summary(_summary())
    with pytest.raises(FileExistsError):
        await store.create_summary(_summary())


@pytest.mark.asyncio
async def test_read_summary_raises_if_missing(store: SummaryStore) -> None:
    with pytest.raises(FileNotFoundError):
        await store.read_summary()


@pytest.mark.asyncio
async def test_read_summary_roundtrip(store: SummaryStore) -> None:
    original = _summary(last_indexed_at=datetime(2026, 1, 1, 12, 0, 0))
    await store.create_summary(original)
    result, _ = await store.read_summary()
    assert result.schema_version == SCHEMA_VERSION
    assert result.last_indexed_at == datetime(2026, 1, 1, 12, 0, 0)


@pytest.mark.asyncio
async def test_write_summary_conflict_raises(store: SummaryStore) -> None:
    summary = _summary()
    version = await store.create_summary(summary)
    # Force some delay
    await asyncio.sleep(0.001)
    await store.write_summary(summary, version)
    with pytest.raises(VersionConflictError):
        await store.write_summary(summary, version)


@pytest.mark.asyncio
async def test_summary_preserves_extra(store: SummaryStore) -> None:
    s = _summary()
    s._extra["futureField"] = "keep-me"
    await store.create_summary(s)
    result, _ = await store.read_summary()
    assert result._extra.get("futureField") == "keep-me"


# ---------------------------------------------------------------------------
# write_full_summary — single-writer-per-session overwrite
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_full_summary_creates_when_absent(store: SummaryStore, tmp_path: Path) -> None:
    await store.write_full_summary(_summary())
    assert (tmp_path / ".ouestcharlie" / "summary.json").exists()


@pytest.mark.asyncio
async def test_write_full_summary_overwrites_when_present(store: SummaryStore) -> None:
    await store.write_full_summary(RootSummary(schema_version=SCHEMA_VERSION))
    await store.write_full_summary(RootSummary(schema_version=SCHEMA_VERSION + 1))
    result, _ = await store.read_summary()
    assert result.schema_version == SCHEMA_VERSION + 1


@pytest.mark.asyncio
async def test_write_full_summary_replaces_legacy_bulky_shape(
    store: SummaryStore, tmp_path: Path
) -> None:
    """A pre-redesign bulky summary.json (with a partitions list) is overwritten cleanly."""
    legacy_path = tmp_path / ".ouestcharlie" / "summary.json"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(
        json.dumps({"schemaVersion": SCHEMA_VERSION, "partitions": [{"path": "2024"}]})
    )

    await store.write_full_summary(RootSummary(schema_version=SCHEMA_VERSION))

    raw = json.loads(legacy_path.read_text())
    assert "partitions" not in raw


@pytest.mark.asyncio
async def test_write_full_summary_preserves_full_content_roundtrip(store: SummaryStore) -> None:
    summary = _summary(last_indexed_at=datetime(2026, 6, 1, 8, 30, 0))
    summary._extra["futureField"] = "keep-me"

    await store.write_full_summary(summary)

    result, _ = await store.read_summary()
    assert result.last_indexed_at == datetime(2026, 6, 1, 8, 30, 0)
    assert result._extra.get("futureField") == "keep-me"


@pytest.mark.asyncio
async def test_write_full_summary_holds_partition_lock(
    store: SummaryStore, backend: LocalBackend
) -> None:
    calls: list[str] = []
    real_partition_lock = backend.partition_lock

    @asynccontextmanager
    async def _recording_lock(partition: str) -> AsyncIterator[PartitionLockToken]:
        calls.append(partition)
        async with real_partition_lock(partition) as token:
            yield token

    backend.partition_lock = _recording_lock  # type: ignore[method-assign]

    await store.write_full_summary(_summary())

    assert calls == [""]


@pytest.mark.asyncio
async def test_write_full_summary_concurrent_writers_no_corruption(
    store: SummaryStore, tmp_path: Path
) -> None:
    a = _summary()
    a._extra["writer"] = "a"
    b = _summary()
    b._extra["writer"] = "b"

    await asyncio.gather(store.write_full_summary(a), store.write_full_summary(b))

    summary_path = tmp_path / ".ouestcharlie" / "summary.json"
    raw = json.loads(summary_path.read_text())
    assert raw.get("writer") in ("a", "b")
