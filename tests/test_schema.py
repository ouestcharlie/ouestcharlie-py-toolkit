"""Test package schema and data models."""

from datetime import datetime

import pytest

from ouestcharlie_toolkit.schema import (
    METADATA_DIR,
    OUESTCHARLIE_NS,
    SCHEMA_VERSION,
    ConfigurationError,
    FileInfo,
    LeafManifest,
    ManifestSummary,
    PhotoEntry,
    VersionConflictError,
    VersionToken,
    XmpSidecar,
    deserialize_leaf,
    manifest_path,
    serialize_leaf,
)

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
    assert photo.searchable == {}
    assert photo.searchable.get("date_taken") is None
    assert photo.searchable.get("make") is None
    assert photo.searchable.get("tags") is None


def test_photo_entry_with_metadata():
    """Test PhotoEntry with full metadata, queryable fields."""
    date = datetime(2024, 7, 15, 14, 30, 0)
    photo = PhotoEntry(
        filename="IMG_001.jpg",
        content_hash="sha256:def456",
        metadata_version=2,
        searchable={
            "date_taken": date,
            "make": "Canon",
            "model": "EOS R5",
            "gps": (48.8566, 2.3522),
            "orientation": 1,
            "tags": ["paris", "vacation"],
            "rating": 4,
            "width": 6000,
            "height": 4000,
        },
    )

    assert photo.filename == "IMG_001.jpg"
    assert photo.searchable["date_taken"] == date
    assert photo.searchable["make"] == "Canon"
    assert photo.searchable["model"] == "EOS R5"
    assert photo.searchable["gps"] == (48.8566, 2.3522)
    assert photo.searchable["orientation"] == 1
    assert photo.searchable["tags"] == ["paris", "vacation"]
    assert photo.searchable["rating"] == 4
    assert photo.searchable["width"] == 6000
    assert photo.searchable["height"] == 4000
    assert photo.metadata_version == 2


def test_photo_entry_optional_fields_default_none():
    """rating, width, height default to None (absent from searchable)."""
    photo = PhotoEntry(filename="test.jpg", content_hash="sha256:abc")
    assert photo.searchable.get("rating") is None
    assert photo.searchable.get("width") is None
    assert photo.searchable.get("height") is None


def test_photo_entry_extra_fields():
    """Test PhotoEntry preserves unknown fields via _extra."""
    photo = PhotoEntry(filename="test.jpg", content_hash="sha256:abc")
    photo._extra["customField"] = "custom value"
    assert photo._extra["customField"] == "custom value"


# ---------------------------------------------------------------------------
# PhotoEntry.from_sidecar
# ---------------------------------------------------------------------------


def test_from_sidecar_basic_fields():
    """filename, content_hash, metadata_version and xmp_version_token are transferred."""
    sidecar = XmpSidecar(content_hash="sha256:abc", metadata_version=3)
    entry = PhotoEntry.from_sidecar("photo.jpg", sidecar, "sha256:abc", "tok1")
    assert entry.filename == "photo.jpg"
    assert entry.content_hash == "sha256:abc"
    assert entry.metadata_version == 3
    assert entry.xmp_version_token == "tok1"


def test_from_sidecar_searchable_fields():
    """Scalar searchable fields (make, model, rating, width, height) are populated."""
    sidecar = XmpSidecar(
        content_hash="sha256:x",
        camera_make="Canon",
        camera_model="EOS R5",
        rating=4,
        width=6000,
        height=4000,
    )
    entry = PhotoEntry.from_sidecar("img.jpg", sidecar, "sha256:x", "v1")
    assert entry.searchable["make"] == "Canon"
    assert entry.searchable["model"] == "EOS R5"
    assert entry.searchable["rating"] == 4
    assert entry.searchable["width"] == 6000
    assert entry.searchable["height"] == 4000


def test_from_sidecar_date_taken():
    """date_taken is placed in searchable under the correct key."""
    dt = datetime(2024, 7, 15, 10, 30)
    sidecar = XmpSidecar(content_hash="sha256:x", date_taken=dt)
    entry = PhotoEntry.from_sidecar("img.jpg", sidecar, "sha256:x", "v1")
    assert entry.searchable["date_taken"] == dt


def test_from_sidecar_gps():
    """GPS tuple is placed in searchable."""
    sidecar = XmpSidecar(content_hash="sha256:x", gps=(48.8566, 2.3522))
    entry = PhotoEntry.from_sidecar("img.jpg", sidecar, "sha256:x", "v1")
    assert entry.searchable["gps"] == (48.8566, 2.3522)


def test_from_sidecar_tags_defensive_copy():
    """tags list is copied so mutations do not affect the sidecar."""
    sidecar = XmpSidecar(content_hash="sha256:x", tags=["a", "b"])
    entry = PhotoEntry.from_sidecar("img.jpg", sidecar, "sha256:x", "v1")
    entry.searchable["tags"].append("c")
    assert sidecar.tags == ["a", "b"]


def test_from_sidecar_none_fields_present():
    """Fields absent on the sidecar produce None values in searchable (not missing keys)."""
    sidecar = XmpSidecar(content_hash="sha256:x")
    entry = PhotoEntry.from_sidecar("img.jpg", sidecar, "sha256:x", "v1")
    # All sidecar-mapped fields appear as None rather than being absent
    assert "rating" in entry.searchable
    assert entry.searchable["rating"] is None


# ---------------------------------------------------------------------------
# Partition summaries
# ---------------------------------------------------------------------------


def test_partition_summary():
    """Test ManifestSummary creation with date and rating ranges."""
    summary = ManifestSummary(
        path="2024/2024-07/",
        photo_count=42,
        _stats={
            "dateTaken": {
                "type": "date_range",
                "min": datetime(2024, 7, 1),
                "max": datetime(2024, 7, 31),
            },
            "rating": {"type": "int_range", "min": 2, "max": 5},
        },
    )

    assert summary.path == "2024/2024-07/"
    assert summary.photo_count == 42
    assert summary.dateTaken["min"] == datetime(2024, 7, 1)
    assert summary.dateTaken["max"] == datetime(2024, 7, 31)
    assert summary.rating["min"] == 2
    assert summary.rating["max"] == 5


def test_partition_summary_rating_defaults_none():
    """rating stat is absent (None) when not provided."""
    summary = ManifestSummary(path="2024/", photo_count=10)
    assert summary.rating is None


# ---------------------------------------------------------------------------
# Manifests
# ---------------------------------------------------------------------------


def test_manifest_path_helper():
    """Test manifest path generation."""
    path = manifest_path("2024/2024-07/")
    assert path == ".ouestcharlie/2024/2024-07/manifest.json"


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
        searchable={
            "make": "Sony",
            "model": "A7 IV",
            "rating": 5,
            "width": 7008,
            "height": 4672,
            "tags": ["sunset"],
            "orientation": 1,
        },
    )
    manifest = LeafManifest(schema_version=SCHEMA_VERSION, partition="p", photos=[photo])
    restored = deserialize_leaf(serialize_leaf(manifest)).photos[0]

    assert restored.searchable["make"] == "Sony"
    assert restored.searchable["model"] == "A7 IV"
    assert restored.searchable["rating"] == 5
    assert restored.searchable["width"] == 7008
    assert restored.searchable["height"] == 4672


def test_photo_entry_rejected_rating_round_trip():
    """rating=-1 (rejected) survives serialize → deserialize."""
    photo = PhotoEntry(filename="x.jpg", content_hash="sha256:abc", searchable={"rating": -1})
    manifest = LeafManifest(schema_version=SCHEMA_VERSION, partition="p", photos=[photo])
    restored = deserialize_leaf(serialize_leaf(manifest)).photos[0]
    assert restored.searchable["rating"] == -1


def test_partition_summary_rating_round_trip():
    """rating and date survive serialize → deserialize with nested stat format."""
    summary = ManifestSummary(
        path="p",
        photo_count=3,
        _stats={
            "dateTaken": {
                "type": "date_range",
                "min": datetime(2024, 1, 1),
                "max": datetime(2024, 12, 31),
            },
            "rating": {"type": "int_range", "min": 1, "max": 5},
        },
    )
    from ouestcharlie_toolkit.schema import _summary_from_dict, _summary_to_dict

    d = _summary_to_dict(summary)
    # Verify nested format
    assert d["dateTaken"] == {
        "type": "date_range",
        "min": "2024-01-01T00:00:00",
        "max": "2024-12-31T00:00:00",
    }
    assert d["rating"] == {"type": "int_range", "min": 1, "max": 5}
    assert "dateMin" not in d and "ratingMin" not in d
    # Verify round-trip
    restored = _summary_from_dict(d)
    assert restored.rating["min"] == 1
    assert restored.rating["max"] == 5
    assert restored.dateTaken["min"] == datetime(2024, 1, 1)
    assert restored.dateTaken["max"] == datetime(2024, 12, 31)


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
