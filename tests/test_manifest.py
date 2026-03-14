"""Tests for ManifestStore — I/O, optimistic concurrency, and roundtrips."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from ouestcharlie_toolkit.backends.local import LocalBackend
from ouestcharlie_toolkit.manifest import ManifestStore
from ouestcharlie_toolkit.schema import (
    METADATA_DIR,
    SCHEMA_VERSION,
    LeafManifest,
    ParentManifest,
    PartitionSummary,
    PhotoEntry,
    VersionConflictError,
    VersionToken,
    manifest_path,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def backend(tmp_path: Path) -> LocalBackend:
    return LocalBackend(root=str(tmp_path))


@pytest.fixture()
def store(backend: LocalBackend) -> ManifestStore:
    return ManifestStore(backend)


def _leaf(partition: str = "2024/2024-07", photos: list[PhotoEntry] | None = None) -> LeafManifest:
    return LeafManifest(
        schema_version=SCHEMA_VERSION,
        partition=partition,
        photos=photos or [PhotoEntry(filename="IMG_001.jpg", content_hash="sha256:abc")],
    )


def _parent(path: str = "2024", children: list[PartitionSummary] | None = None) -> ParentManifest:
    return ParentManifest(
        schema_version=SCHEMA_VERSION,
        path=path,
        children=children or [PartitionSummary(path="2024/2024-07", photo_count=1)],
    )


# ---------------------------------------------------------------------------
# Leaf — create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_leaf_writes_file(store: ManifestStore, tmp_path: Path) -> None:
    await store.create_leaf(_leaf())
    expected = tmp_path / "2024" / "2024-07" / METADATA_DIR / "manifest.json"
    assert expected.exists()


@pytest.mark.asyncio
async def test_create_leaf_valid_json(store: ManifestStore, tmp_path: Path) -> None:
    await store.create_leaf(_leaf())
    path = tmp_path / "2024" / "2024-07" / METADATA_DIR / "manifest.json"
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
    await store.create_leaf(_leaf())
    manifest, version = await store.read_leaf("2024/2024-07")

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
        date_taken=date,
        make="Canon",
        model="EOS R5",
        gps=(48.8566, 2.3522),
        orientation=1,
        tags=["paris", "vacation"],
        metadata_version=3,
        xmp_version_token="1234567890",
    )
    await store.create_leaf(LeafManifest(schema_version=SCHEMA_VERSION, partition="p", photos=[photo]))
    manifest, _ = await store.read_leaf("p")
    e = manifest.photos[0]
    assert e.date_taken == date
    assert e.make == "Canon"
    assert e.model == "EOS R5"
    assert e.gps == (48.8566, 2.3522)
    assert e.orientation == 1
    assert e.tags == ["paris", "vacation"]
    assert e.metadata_version == 3
    assert e.xmp_version_token == "1234567890"


# ---------------------------------------------------------------------------
# Leaf — summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_leaf_summary_roundtrip(store: ManifestStore) -> None:
    leaf = _leaf()
    leaf.summary = PartitionSummary(
        path="2024/2024-07",
        photo_count=42,
        _stats={"date": {"type": "date_range", "min": datetime(2024, 7, 1), "max": datetime(2024, 7, 31)}},
    )
    await store.create_leaf(leaf)
    manifest, _ = await store.read_leaf("2024/2024-07")
    assert manifest.summary is not None
    assert manifest.summary.photo_count == 42
    assert manifest.summary.date["min"] == datetime(2024, 7, 1)
    assert manifest.summary.date["max"] == datetime(2024, 7, 31)


# ---------------------------------------------------------------------------
# Parent — create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_parent_writes_file(store: ManifestStore, tmp_path: Path) -> None:
    await store.create_parent(_parent())
    expected = tmp_path / "2024" / METADATA_DIR / "manifest.json"
    assert expected.exists()


@pytest.mark.asyncio
async def test_create_parent_raises_if_exists(store: ManifestStore) -> None:
    await store.create_parent(_parent())
    with pytest.raises(FileExistsError):
        await store.create_parent(_parent())


# ---------------------------------------------------------------------------
# Parent — read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_parent_raises_if_missing(store: ManifestStore) -> None:
    with pytest.raises(FileNotFoundError):
        await store.read_parent("2024")


@pytest.mark.asyncio
async def test_read_parent_roundtrip(store: ManifestStore) -> None:
    await store.create_parent(_parent())
    manifest, version = await store.read_parent("2024")
    assert manifest.path == "2024"
    assert manifest.schema_version == SCHEMA_VERSION
    assert len(manifest.children) == 1
    assert manifest.children[0].path == "2024/2024-07"
    assert isinstance(version, VersionToken)


@pytest.mark.asyncio
async def test_read_parent_preserves_extra(store: ManifestStore) -> None:
    parent = _parent()
    parent._extra["futureField"] = "v2-data"
    await store.create_parent(parent)
    manifest, _ = await store.read_parent("2024")
    assert manifest._extra.get("futureField") == "v2-data"


# ---------------------------------------------------------------------------
# Parent — write (optimistic concurrency)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_parent_updates_content(store: ManifestStore) -> None:
    await store.create_parent(_parent())
    manifest, version = await store.read_parent("2024")

    manifest.children.append(PartitionSummary(path="2024/2024-08", photo_count=5))
    await store.write_parent(manifest, version)

    updated, _ = await store.read_parent("2024")
    assert len(updated.children) == 2


@pytest.mark.asyncio
async def test_write_parent_conflict_raises(store: ManifestStore) -> None:
    await store.create_parent(_parent())
    manifest, version = await store.read_parent("2024")
    await store.write_parent(manifest, version)

    with pytest.raises(VersionConflictError):
        await store.write_parent(manifest, version)


# ---------------------------------------------------------------------------
# rebuild_parent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rebuild_parent_creates_when_absent(store: ManifestStore, tmp_path: Path) -> None:
    summaries = [PartitionSummary(path="2024/2024-07", photo_count=10)]
    manifest = await store.rebuild_parent("2024", summaries)
    assert manifest.path == "2024"
    assert len(manifest.children) == 1
    expected = tmp_path / "2024" / METADATA_DIR / "manifest.json"
    assert expected.exists()


@pytest.mark.asyncio
async def test_rebuild_parent_updates_when_present(store: ManifestStore) -> None:
    await store.create_parent(_parent(children=[PartitionSummary(path="2024/2024-07", photo_count=1)]))

    summaries = [
        PartitionSummary(path="2024/2024-07", photo_count=50),
        PartitionSummary(path="2024/2024-08", photo_count=30),
    ]
    manifest = await store.rebuild_parent("2024", summaries)
    assert len(manifest.children) == 2
    assert manifest.children[0].photo_count == 50


@pytest.mark.asyncio
async def test_rebuild_parent_preserves_extra(store: ManifestStore) -> None:
    parent = _parent()
    parent._extra["legacyField"] = "keep-me"
    await store.create_parent(parent)

    manifest = await store.rebuild_parent("2024", [PartitionSummary(path="2024/2024-07", photo_count=5)])
    assert manifest._extra.get("legacyField") == "keep-me"
