"""Shared data models, exceptions, and constants for the OuEstCharlie toolkit."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OUESTCHARLIE_NS = "http://ouestcharlie.app/ns/1.0/"
SCHEMA_VERSION = 1
MANIFEST_FILENAME = "manifest.json"
METADATA_DIR = ".ouestcharlie"


def manifest_path(partition: str) -> str:
    """Well-known manifest path for a partition, e.g. '2024/2024-07/' -> '2024/2024-07/.ouestcharlie/manifest.json'."""
    prefix = partition.rstrip("/") + "/" if partition else ""
    return f"{prefix}{METADATA_DIR}/{MANIFEST_FILENAME}"


# ---------------------------------------------------------------------------
# Version token (opaque to callers)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VersionToken:
    """Opaque version token returned by backends. Callers pass it back to
    write_conditional without inspecting its value."""

    value: Any


@dataclass(frozen=True)
class FileInfo:
    """Metadata about a file returned by Backend.list_files."""

    path: str
    version: VersionToken


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class VersionConflictError(Exception):
    """Raised when a conditional write fails because the file was modified."""

    def __init__(self, path: str, expected: VersionToken, actual: VersionToken) -> None:
        self.path = path
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"Version conflict on {path}: expected {expected.value}, got {actual.value}"
        )


class ConfigurationError(Exception):
    """Raised for invalid or missing configuration (backend root missing, bad credentials, etc.)."""


# ---------------------------------------------------------------------------
# Photo entry (leaf manifest)
# ---------------------------------------------------------------------------


@dataclass
class PhotoEntry:
    """Per-photo metadata entry within a leaf manifest."""

    filename: str
    content_hash: str  # e.g. "sha256:a1b2c3..."
    date_taken: datetime | None = None
    camera: str | None = None
    gps: tuple[float, float] | None = None
    orientation: int | None = None
    tags: list[str] = field(default_factory=list)
    metadata_version: int = 1
    xmp_version_token: str = ""  # backend version token at consolidation time
    _extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Partition summary
# ---------------------------------------------------------------------------


@dataclass
class PartitionSummary:
    """Summary statistics for a partition, used in parent manifests and as
    the summary block of leaf manifests."""

    path: str
    photo_count: int = 0
    date_min: datetime | None = None
    date_max: datetime | None = None
    tags_bloom: bytes = b""  # serialized bloom filter over all tags
    hashes_bloom: bytes = b""  # serialized bloom filter over content hashes
    _extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Manifests
# ---------------------------------------------------------------------------


@dataclass
class LeafManifest:
    """Leaf-level manifest containing full per-photo metadata inline."""

    schema_version: int
    partition: str
    photos: list[PhotoEntry] = field(default_factory=list)
    summary: PartitionSummary | None = None
    thumbnails_hash: str | None = None
    previews_hash: str | None = None
    _extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParentManifest:
    """Parent manifest that consolidates child partition summaries."""

    schema_version: int
    path: str
    children: list[PartitionSummary] = field(default_factory=list)
    _extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# XMP sidecar
# ---------------------------------------------------------------------------


@dataclass
class XmpSidecar:
    """Parsed representation of an XMP sidecar file."""

    content_hash: str | None = None
    metadata_version: int = 1
    schema_version: int = SCHEMA_VERSION
    date_taken: datetime | None = None
    gps: tuple[float, float] | None = None
    camera_make: str | None = None
    camera_model: str | None = None
    orientation: int | None = None
    tags: list[str] = field(default_factory=list)
    _raw_xml: str = ""  # preserved for round-tripping unknown fields / namespaces


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

# These convert between dataclass instances and JSON-compatible dicts.
# Unknown fields are preserved via the _extra attribute.


def _photo_entry_to_dict(entry: PhotoEntry) -> dict[str, Any]:
    d: dict[str, Any] = {
        "filename": entry.filename,
        "contentHash": entry.content_hash,
        "metadataVersion": entry.metadata_version,
        "xmpVersionToken": entry.xmp_version_token,
    }
    if entry.date_taken is not None:
        d["dateTaken"] = entry.date_taken.isoformat()
    if entry.camera is not None:
        d["camera"] = entry.camera
    if entry.gps is not None:
        d["gps"] = list(entry.gps)
    if entry.orientation is not None:
        d["orientation"] = entry.orientation
    if entry.tags:
        d["tags"] = entry.tags
    d.update(entry._extra)
    return d


def _photo_entry_from_dict(d: dict[str, Any]) -> PhotoEntry:
    known_keys = {
        "filename", "contentHash", "dateTaken", "camera", "gps",
        "orientation", "tags", "metadataVersion", "xmpVersionToken",
    }
    extra = {k: v for k, v in d.items() if k not in known_keys}
    gps_raw = d.get("gps")
    return PhotoEntry(
        filename=d["filename"],
        content_hash=d["contentHash"],
        date_taken=datetime.fromisoformat(d["dateTaken"]) if d.get("dateTaken") else None,
        camera=d.get("camera"),
        gps=tuple(gps_raw) if gps_raw else None,  # type: ignore[arg-type]
        orientation=d.get("orientation"),
        tags=d.get("tags", []),
        metadata_version=d.get("metadataVersion", 1),
        xmp_version_token=d.get("xmpVersionToken", ""),
        _extra=extra,
    )


def _summary_to_dict(s: PartitionSummary) -> dict[str, Any]:
    d: dict[str, Any] = {
        "path": s.path,
        "photoCount": s.photo_count,
    }
    if s.date_min is not None:
        d["dateMin"] = s.date_min.isoformat()
    if s.date_max is not None:
        d["dateMax"] = s.date_max.isoformat()
    if s.tags_bloom:
        # TODO: base64 encode bloom filters
        d["tagsBloom"] = s.tags_bloom.hex()
    if s.hashes_bloom:
        d["hashesBloom"] = s.hashes_bloom.hex()
    d.update(s._extra)
    return d


def _summary_from_dict(d: dict[str, Any]) -> PartitionSummary:
    known_keys = {"path", "photoCount", "dateMin", "dateMax", "tagsBloom", "hashesBloom"}
    extra = {k: v for k, v in d.items() if k not in known_keys}
    return PartitionSummary(
        path=d["path"],
        photo_count=d.get("photoCount", 0),
        date_min=datetime.fromisoformat(d["dateMin"]) if d.get("dateMin") else None,
        date_max=datetime.fromisoformat(d["dateMax"]) if d.get("dateMax") else None,
        tags_bloom=bytes.fromhex(d["tagsBloom"]) if d.get("tagsBloom") else b"",
        hashes_bloom=bytes.fromhex(d["hashesBloom"]) if d.get("hashesBloom") else b"",
        _extra=extra,
    )


def serialize_leaf(manifest: LeafManifest) -> dict[str, Any]:
    """Serialize a LeafManifest to a JSON-compatible dict."""
    d: dict[str, Any] = {
        "schemaVersion": manifest.schema_version,
        "partition": manifest.partition,
        "photos": [_photo_entry_to_dict(p) for p in manifest.photos],
    }
    if manifest.summary is not None:
        d["summary"] = _summary_to_dict(manifest.summary)
    if manifest.thumbnails_hash is not None:
        d["thumbnailsHash"] = manifest.thumbnails_hash
    if manifest.previews_hash is not None:
        d["previewsHash"] = manifest.previews_hash
    d.update(manifest._extra)
    return d


def deserialize_leaf(d: dict[str, Any]) -> LeafManifest:
    """Deserialize a JSON dict into a LeafManifest, preserving unknown fields."""
    known_keys = {
        "schemaVersion", "partition", "photos", "summary",
        "thumbnailsHash", "previewsHash",
    }
    extra = {k: v for k, v in d.items() if k not in known_keys}
    return LeafManifest(
        schema_version=d.get("schemaVersion", SCHEMA_VERSION),
        partition=d["partition"],
        photos=[_photo_entry_from_dict(p) for p in d.get("photos", [])],
        summary=_summary_from_dict(d["summary"]) if d.get("summary") else None,
        thumbnails_hash=d.get("thumbnailsHash"),
        previews_hash=d.get("previewsHash"),
        _extra=extra,
    )


def serialize_parent(manifest: ParentManifest) -> dict[str, Any]:
    """Serialize a ParentManifest to a JSON-compatible dict."""
    d: dict[str, Any] = {
        "schemaVersion": manifest.schema_version,
        "path": manifest.path,
        "children": [_summary_to_dict(c) for c in manifest.children],
    }
    d.update(manifest._extra)
    return d


def deserialize_parent(d: dict[str, Any]) -> ParentManifest:
    """Deserialize a JSON dict into a ParentManifest, preserving unknown fields."""
    known_keys = {"schemaVersion", "path", "children"}
    extra = {k: v for k, v in d.items() if k not in known_keys}
    return ParentManifest(
        schema_version=d.get("schemaVersion", SCHEMA_VERSION),
        path=d["path"],
        children=[_summary_from_dict(c) for c in d.get("children", [])],
        _extra=extra,
    )
