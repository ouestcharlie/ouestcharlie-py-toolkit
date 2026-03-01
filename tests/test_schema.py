"""Test package schema and data models."""

from datetime import datetime

from ouestcharlie_toolkit.schema import (
    VersionToken,
    FileInfo,
    PhotoEntry,
    PartitionSummary,
    LeafManifest,
    ParentManifest,
    XmpSidecar,
    VersionConflictError,
    ConfigurationError,
    serialize_leaf,
    deserialize_leaf,
    serialize_parent,
    deserialize_parent,
    manifest_path,
    SCHEMA_VERSION,
    OUESTCHARLIE_NS,
    METADATA_DIR,
)
import pytest


# ---------------------------------------------------------------------------
# Version tokens and file info
# ---------------------------------------------------------------------------


def test_version_token():
    """Test VersionToken creation and access."""
    token = VersionToken(12345)
    assert token.value == 12345


def test_version_token_equality():
    """Test VersionToken equality comparison."""
    token1 = VersionToken(12345)
    token2 = VersionToken(12345)
    token3 = VersionToken(54321)
    assert token1 == token2
    assert token1 != token3


def test_file_info():
    """Test FileInfo creation."""
    token = VersionToken("etag-abc123")
    info = FileInfo(path="2024/photo.jpg", version=token)
    assert info.path == "2024/photo.jpg"
    assert info.version == token


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


def test_version_conflict_error():
    """Test VersionConflictError creation and attributes."""
    expected = VersionToken("v1")
    actual = VersionToken("v2")
    error = VersionConflictError("test.jpg", expected, actual)

    assert error.path == "test.jpg"
    assert error.expected == expected
    assert error.actual == actual
    assert "test.jpg" in str(error)
    assert "v1" in str(error)
    assert "v2" in str(error)


def test_configuration_error():
    """Test ConfigurationError can be raised."""
    with pytest.raises(ConfigurationError):
        raise ConfigurationError("Invalid config")


# ---------------------------------------------------------------------------
# Photo entries
# ---------------------------------------------------------------------------


def test_photo_entry_minimal():
    """Test PhotoEntry with minimal required fields."""
    photo = PhotoEntry(filename="test.jpg", content_hash="sha256:abc123")
    assert photo.filename == "test.jpg"
    assert photo.content_hash == "sha256:abc123"
    assert photo.date_taken is None
    assert photo.make is None
    assert photo.model is None
    assert photo.gps is None
    assert photo.tags == []


def test_photo_entry_with_metadata():
    """Test PhotoEntry with full metadata, queryable fields."""
    date = datetime(2024, 7, 15, 14, 30, 0)
    photo = PhotoEntry(
        filename="IMG_001.jpg",
        content_hash="sha256:def456",
        date_taken=date,
        make="Canon",
        model="EOS R5",
        gps=(48.8566, 2.3522),
        orientation=1,
        tags=["paris", "vacation"],
        rating=4,
        width=6000,
        height=4000,
        metadata_version=2,
    )

    assert photo.filename == "IMG_001.jpg"
    assert photo.date_taken == date
    assert photo.make == "Canon"
    assert photo.model == "EOS R5"
    assert photo.gps == (48.8566, 2.3522)
    assert photo.orientation == 1
    assert photo.tags == ["paris", "vacation"]
    assert photo.rating == 4
    assert photo.width == 6000
    assert photo.height == 4000
    assert photo.metadata_version == 2


def test_photo_entry_optional_fields_default_none():
    """rating, width, height default to None."""
    photo = PhotoEntry(filename="test.jpg", content_hash="sha256:abc")
    assert photo.rating is None
    assert photo.width is None
    assert photo.height is None


def test_photo_entry_extra_fields():
    """Test PhotoEntry preserves unknown fields via _extra."""
    photo = PhotoEntry(filename="test.jpg", content_hash="sha256:abc")
    photo._extra["customField"] = "custom value"
    assert photo._extra["customField"] == "custom value"


# ---------------------------------------------------------------------------
# Partition summaries
# ---------------------------------------------------------------------------


def test_partition_summary():
    """Test PartitionSummary creation with date and rating ranges."""
    summary = PartitionSummary(
        path="2024/2024-07/",
        photo_count=42,
        date_min=datetime(2024, 7, 1),
        date_max=datetime(2024, 7, 31),
        rating_min=2,
        rating_max=5,
    )

    assert summary.path == "2024/2024-07/"
    assert summary.photo_count == 42
    assert summary.date_min == datetime(2024, 7, 1)
    assert summary.date_max == datetime(2024, 7, 31)
    assert summary.rating_min == 2
    assert summary.rating_max == 5


def test_partition_summary_rating_defaults_none():
    """rating_min and rating_max default to None when absent."""
    summary = PartitionSummary(path="2024/", photo_count=10)
    assert summary.rating_min is None
    assert summary.rating_max is None


# ---------------------------------------------------------------------------
# Manifests
# ---------------------------------------------------------------------------


def test_manifest_path_helper():
    """Test manifest path generation."""
    path = manifest_path("2024/2024-07/")
    assert path == "2024/2024-07/.ouestcharlie/manifest.json"


def test_manifest_path_empty():
    """Test manifest path for root partition."""
    path = manifest_path("")
    assert path == ".ouestcharlie/manifest.json"


def test_leaf_manifest_creation():
    """Test LeafManifest creation."""
    photo = PhotoEntry(filename="test.jpg", content_hash="sha256:abc123")
    manifest = LeafManifest(
        schema_version=SCHEMA_VERSION,
        partition="2024/2024-07/",
        photos=[photo],
    )

    assert manifest.schema_version == SCHEMA_VERSION
    assert manifest.partition == "2024/2024-07/"
    assert len(manifest.photos) == 1
    assert manifest.photos[0] == photo
    assert manifest.summary is None


def test_leaf_manifest_serialization():
    """Test LeafManifest serialization."""
    photo = PhotoEntry(filename="test.jpg", content_hash="sha256:abc123")
    manifest = LeafManifest(
        schema_version=SCHEMA_VERSION,
        partition="2024/2024-07/",
        photos=[photo],
    )

    serialized = serialize_leaf(manifest)
    assert serialized["partition"] == "2024/2024-07/"
    assert serialized["schemaVersion"] == SCHEMA_VERSION
    assert len(serialized["photos"]) == 1
    assert serialized["photos"][0]["filename"] == "test.jpg"


def test_leaf_manifest_deserialization():
    """Test LeafManifest deserialization round-trip."""
    photo = PhotoEntry(filename="test.jpg", content_hash="sha256:abc123")
    manifest = LeafManifest(
        schema_version=SCHEMA_VERSION,
        partition="2024/2024-07/",
        photos=[photo],
    )

    serialized = serialize_leaf(manifest)
    deserialized = deserialize_leaf(serialized)

    assert deserialized.partition == "2024/2024-07/"
    assert deserialized.schema_version == SCHEMA_VERSION
    assert len(deserialized.photos) == 1
    assert deserialized.photos[0].filename == "test.jpg"
    assert deserialized.photos[0].content_hash == "sha256:abc123"


def test_photo_entry_v1_fields_round_trip():
    """All queryable fields survive serialize → deserialize."""
    photo = PhotoEntry(
        filename="IMG_001.jpg",
        content_hash="sha256:abc",
        make="Sony",
        model="A7 IV",
        rating=5,
        width=7008,
        height=4672,
        tags=["sunset"],
        orientation=1,
    )
    manifest = LeafManifest(schema_version=SCHEMA_VERSION, partition="p", photos=[photo])
    restored = deserialize_leaf(serialize_leaf(manifest)).photos[0]

    assert restored.make == "Sony"
    assert restored.model == "A7 IV"
    assert restored.rating == 5
    assert restored.width == 7008
    assert restored.height == 4672


def test_photo_entry_rejected_rating_round_trip():
    """rating=-1 (rejected) survives serialize → deserialize."""
    photo = PhotoEntry(filename="x.jpg", content_hash="sha256:abc", rating=-1)
    manifest = LeafManifest(schema_version=SCHEMA_VERSION, partition="p", photos=[photo])
    restored = deserialize_leaf(serialize_leaf(manifest)).photos[0]
    assert restored.rating == -1


def test_partition_summary_rating_round_trip():
    """ratingMin / ratingMax survive serialize → deserialize."""
    summary = PartitionSummary(
        path="p", photo_count=3,
        date_min=datetime(2024, 1, 1), date_max=datetime(2024, 12, 31),
        rating_min=1, rating_max=5,
    )
    from ouestcharlie_toolkit.schema import _summary_to_dict, _summary_from_dict
    restored = _summary_from_dict(_summary_to_dict(summary))
    assert restored.rating_min == 1
    assert restored.rating_max == 5


def test_parent_manifest_creation():
    """Test ParentManifest creation."""
    child1 = PartitionSummary(path="2024/2024-07/", photo_count=10)
    child2 = PartitionSummary(path="2024/2024-08/", photo_count=15)

    parent = ParentManifest(
        schema_version=SCHEMA_VERSION,
        path="2024/",
        children=[child1, child2],
    )

    assert parent.schema_version == SCHEMA_VERSION
    assert parent.path == "2024/"
    assert len(parent.children) == 2
    assert parent.children[0].photo_count == 10
    assert parent.children[1].photo_count == 15


def test_parent_manifest_serialization():
    """Test ParentManifest serialization round-trip."""
    child = PartitionSummary(path="2024/2024-07/", photo_count=5)
    parent = ParentManifest(
        schema_version=SCHEMA_VERSION,
        path="2024/",
        children=[child],
    )

    serialized = serialize_parent(parent)
    deserialized = deserialize_parent(serialized)

    assert deserialized.path == "2024/"
    assert deserialized.schema_version == SCHEMA_VERSION
    assert len(deserialized.children) == 1
    assert deserialized.children[0].path == "2024/2024-07/"
    assert deserialized.children[0].photo_count == 5


# ---------------------------------------------------------------------------
# XMP sidecars
# ---------------------------------------------------------------------------


def test_xmp_sidecar_creation():
    """Test XmpSidecar creation including queryable fields."""
    xmp = XmpSidecar(
        content_hash="sha256:xyz789",
        date_taken=datetime(2024, 7, 15),
        camera_make="Canon",
        camera_model="EOS R5",
        tags=["vacation", "paris"],
        rating=4,
        width=6000,
        height=4000,
    )

    assert xmp.content_hash == "sha256:xyz789"
    assert xmp.date_taken == datetime(2024, 7, 15)
    assert xmp.camera_make == "Canon"
    assert xmp.camera_model == "EOS R5"
    assert xmp.tags == ["vacation", "paris"]
    assert xmp.schema_version == SCHEMA_VERSION
    assert xmp.rating == 4
    assert xmp.width == 6000
    assert xmp.height == 4000


def test_xmp_sidecar_optional_fields_default_none():
    """rating, width, height default to None on XmpSidecar."""
    xmp = XmpSidecar()
    assert xmp.rating is None
    assert xmp.width is None
    assert xmp.height is None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_constants():
    """Test that constants are defined correctly."""
    assert OUESTCHARLIE_NS == "http://ouestcharlie.app/ns/1.0/"
    assert SCHEMA_VERSION == 1
    assert METADATA_DIR == ".ouestcharlie"
