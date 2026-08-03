"""Tests for ManifestStore — thin root summary I/O and optimistic concurrency."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

import pytest

from ouestcharlie_toolkit.backend import VersionConflictError
from ouestcharlie_toolkit.backends.local import LocalBackend
from ouestcharlie_toolkit.manifest import ManifestStore
from ouestcharlie_toolkit.schema import (
    SCHEMA_VERSION,
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
# RootSummary (summary.json) — thin marker shape
# ---------------------------------------------------------------------------


def _summary(last_indexed_at: datetime | None = None) -> RootSummary:
    return RootSummary(schema_version=SCHEMA_VERSION, last_indexed_at=last_indexed_at)


@pytest.mark.asyncio
async def test_create_summary_writes_file(store: ManifestStore, tmp_path: Path) -> None:
    await store.create_summary(_summary())
    expected = tmp_path / ".ouestcharlie" / "summary.json"
    assert expected.exists()
    raw = json.loads(expected.read_text())
    assert raw["schemaVersion"] == SCHEMA_VERSION
    assert "partitions" not in raw


@pytest.mark.asyncio
async def test_create_summary_raises_if_exists(store: ManifestStore) -> None:
    await store.create_summary(_summary())
    with pytest.raises(FileExistsError):
        await store.create_summary(_summary())


@pytest.mark.asyncio
async def test_read_summary_raises_if_missing(store: ManifestStore) -> None:
    with pytest.raises(FileNotFoundError):
        await store.read_summary()


@pytest.mark.asyncio
async def test_read_summary_roundtrip(store: ManifestStore) -> None:
    original = _summary(last_indexed_at=datetime(2026, 1, 1, 12, 0, 0))
    await store.create_summary(original)
    result, _ = await store.read_summary()
    assert result.schema_version == SCHEMA_VERSION
    assert result.last_indexed_at == datetime(2026, 1, 1, 12, 0, 0)


@pytest.mark.asyncio
async def test_write_summary_conflict_raises(store: ManifestStore) -> None:
    summary = _summary()
    version = await store.create_summary(summary)
    # Force some delay
    await asyncio.sleep(0.001)
    await store.write_summary(summary, version)
    with pytest.raises(VersionConflictError):
        await store.write_summary(summary, version)


@pytest.mark.asyncio
async def test_summary_preserves_extra(store: ManifestStore) -> None:
    s = _summary()
    s._extra["futureField"] = "keep-me"
    await store.create_summary(s)
    result, _ = await store.read_summary()
    assert result._extra.get("futureField") == "keep-me"


# ---------------------------------------------------------------------------
# write_full_summary — single-writer-per-session overwrite
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_full_summary_creates_when_absent(store: ManifestStore, tmp_path: Path) -> None:
    await store.write_full_summary(_summary())
    assert (tmp_path / ".ouestcharlie" / "summary.json").exists()


@pytest.mark.asyncio
async def test_write_full_summary_overwrites_when_present(store: ManifestStore) -> None:
    await store.write_full_summary(RootSummary(schema_version=SCHEMA_VERSION))
    await store.write_full_summary(RootSummary(schema_version=SCHEMA_VERSION + 1))
    result, _ = await store.read_summary()
    assert result.schema_version == SCHEMA_VERSION + 1


@pytest.mark.asyncio
async def test_write_full_summary_replaces_legacy_bulky_shape(
    store: ManifestStore, tmp_path: Path
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
