"""Tests for ManifestStore — I/O, optimistic concurrency, and roundtrips."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

import pytest

from ouestcharlie_toolkit.backend import VersionConflictError, VersionToken
from ouestcharlie_toolkit.backends.local import LocalBackend
from ouestcharlie_toolkit.manifest import ManifestStore
from ouestcharlie_toolkit.schema import (
    METADATA_DIR,
    SCHEMA_VERSION,
    LeafManifest,
    ManifestSummary,
    PhotoEntry,
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


def _leaf(partition: str = "2024/2024-07", photos: list[PhotoEntry] | None = None) -> LeafManifest:
    return LeafManifest(
        schema_version=SCHEMA_VERSION,
        partition=partition,
        photos=photos or [PhotoEntry(filename="IMG_001.jpg", content_hash="sha256:abc")],
    )


# ---------------------------------------------------------------------------
# Leaf — create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_leaf_writes_file(store: ManifestStore, tmp_path: Path) -> None:
    await store.create_leaf(_leaf())
    expected = tmp_path / METADATA_DIR / "2024" / "2024-07" / "manifest.json"
    assert expected.exists()


@pytest.mark.asyncio
async def test_create_leaf_valid_json(store: ManifestStore, tmp_path: Path) -> None:
    await store.create_leaf(_leaf())
    path = tmp_path / METADATA_DIR / "2024" / "2024-07" / "manifest.json"
    data = json.loads(path.read_text())
    assert data["partition"] == "2024/2024-07"
    assert data["schemaVersion"] == SCHEMA_VERSION


@pytest.mark.asyncio
async def test_create_leaf_root_partition(store: ManifestStore, tmp_path: Path) -> None:
    await store.create_leaf(_leaf(partition=""))
    expected = tmp_path / METADATA_DIR / "manifest.json"
    assert expected.exists()


@pytest.mark.asyncio
async def test_create_leaf_raises_if_exists(store: ManifestStore) -> None:
    await store.create_leaf(_leaf())
    with pytest.raises(FileExistsError):
        await store.create_leaf(_leaf())


# ---------------------------------------------------------------------------
# Leaf — read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_leaf_raises_if_missing(store: ManifestStore) -> None:
    with pytest.raises(FileNotFoundError):
        await store.read_leaf("2024/2024-07")


@pytest.mark.asyncio
async def test_read_leaf_roundtrip(store: ManifestStore) -> None:
    original = _leaf()
    await store.create_leaf(original)
    manifest, version = await store.read_leaf("2024/2024-07")
    assert manifest.partition == "2024/2024-07"
    assert manifest.schema_version == SCHEMA_VERSION
    assert len(manifest.photos) == 1
    assert manifest.photos[0].filename == "IMG_001.jpg"
    assert manifest.photos[0].content_hash == "sha256:abc"
    assert isinstance(version, VersionToken)


@pytest.mark.asyncio
async def test_read_leaf_preserves_extra(store: ManifestStore) -> None:
    leaf = _leaf()
    leaf._extra["futureField"] = "v2-data"
    await store.create_leaf(leaf)
    manifest, _ = await store.read_leaf("2024/2024-07")
    assert manifest._extra.get("futureField") == "v2-data"


# ---------------------------------------------------------------------------
# Leaf — write (optimistic concurrency)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_leaf_updates_content(store: ManifestStore) -> None:
    await store.create_leaf(_leaf())
    manifest, version = await store.read_leaf("2024/2024-07")

    manifest.photos.append(PhotoEntry(filename="IMG_002.jpg", content_hash="sha256:def"))
    await store.write_leaf(manifest, version)

    updated, _ = await store.read_leaf("2024/2024-07")
    assert len(updated.photos) == 2


@pytest.mark.asyncio
async def test_write_leaf_returns_new_version(store: ManifestStore) -> None:
    await store.create_leaf(_leaf())
    manifest, v1 = await store.read_leaf("2024/2024-07")
    v2 = await store.write_leaf(manifest, v1)
    assert isinstance(v2, VersionToken)


@pytest.mark.asyncio
async def test_write_leaf_conflict_raises(store: ManifestStore) -> None:
    import asyncio

    await store.create_leaf(_leaf())
    manifest, version = await store.read_leaf("2024/2024-07")

    # Brief pause so the next write lands on a different mtime tick even on
    # coarse-resolution filesystems (e.g. tmpfs in CI).
    await asyncio.sleep(0.01)

    # Simulate a concurrent write that advances the version.
    await store.write_leaf(manifest, version)

    # Now try to write with the original (stale) version.
    with pytest.raises(VersionConflictError):
        await store.write_leaf(manifest, version)


# ---------------------------------------------------------------------------
# Leaf — read_modify_write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_modify_write_leaf(store: ManifestStore) -> None:
    await store.create_leaf(_leaf())

    def add_photo(m: LeafManifest) -> LeafManifest:
        m.photos.append(PhotoEntry(filename="IMG_002.jpg", content_hash="sha256:def"))
        return m

    updated = await store.read_modify_write_leaf("2024/2024-07", add_photo)
    assert len(updated.photos) == 2


@pytest.mark.asyncio
async def test_read_modify_write_leaf_missing_raises(store: ManifestStore) -> None:
    with pytest.raises(FileNotFoundError):
        await store.read_modify_write_leaf("2024/2024-07", lambda m: m)


# ---------------------------------------------------------------------------
# Leaf — photo entry fields survive roundtrip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_leaf_photo_entry_full_roundtrip(store: ManifestStore) -> None:
    date = datetime(2024, 7, 15, 14, 30, 0)
    photo = PhotoEntry(
        filename="IMG_001.jpg",
        content_hash="sha256:abc",
        metadata_version=3,
        xmp_version_token="1234567890",
        searchable={
            "date_taken": date,
            "make": "Canon",
            "model": "EOS R5",
            "gps": (48.8566, 2.3522),
            "orientation": 1,
            "tags": ["paris", "vacation"],
        },
    )
    await store.create_leaf(
        LeafManifest(schema_version=SCHEMA_VERSION, partition="p", photos=[photo])
    )
    manifest, _ = await store.read_leaf("p")
    e = manifest.photos[0]
    assert e.searchable["date_taken"] == date
    assert e.searchable["make"] == "Canon"
    assert e.searchable["model"] == "EOS R5"
    assert e.searchable["gps"] == (48.8566, 2.3522)
    assert e.searchable["orientation"] == 1
    assert e.searchable["tags"] == ["paris", "vacation"]
    assert e.metadata_version == 3
    assert e.xmp_version_token == "1234567890"


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
