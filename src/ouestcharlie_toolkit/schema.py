"""Shared data models, exceptions, and constants for the OuEstCharlie toolkit."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ouestcharlie_toolkit.fields import PHOTO_FIELDS, FieldType


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
    """Per-photo metadata entry within a leaf manifest.

    Searchable metadata (driven by PHOTO_FIELDS) is stored in ``searchable``
    keyed by ``FieldDef.entry_attr``.  Unknown XMP fields are preserved in
    ``_extra``.
    """

    filename: str
    content_hash: str
    searchable: dict[str, Any] = field(default_factory=dict)
    metadata_version: int = 1
    xmp_version_token: str = ""
    _extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Partition summary helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Partition summary
# ---------------------------------------------------------------------------


class PartitionSummary:
    """Summary statistics for a partition, used in parent manifests and as
    the summary block of leaf manifests.

    Per-field statistics are stored in ``_stats`` as typed dicts that mirror
    the JSON serialisation format:

    - date range:  ``{"type": "date_range", "min": datetime, "max": datetime}``
    - int range:   ``{"type": "int_range",  "min": int,      "max": int}``
    - bloom:       ``{"type": "bloom",      "value": bytes}``

    Field stats are accessed via normal attribute syntax (``__getattr__``),
    e.g. ``summary.date["min"]``, ``summary.rating["max"]``.

    Adding a new summarisable field requires only a ``FieldDef`` entry in
    ``fields.py`` — no changes needed here.
    """

    def __init__(
        self,
        path: str,
        photo_count: int = 0,
        _stats: dict[str, dict[str, Any]] | None = None,
        _extra: dict[str, Any] | None = None,
    ) -> None:
        self.path = path
        self.photo_count = photo_count
        self._stats: dict[str, dict[str, Any]] = dict(_stats) if _stats else {}
        self._extra: dict[str, Any] = dict(_extra) if _extra is not None else {}

    def __getattr__(self, name: str) -> Any:
        """Return the typed stat dict for a field, e.g. summary.rating → {"type": "int_range", ...}."""
        return self.__dict__.get("_stats", {}).get(name)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PartitionSummary):
            return NotImplemented
        return (
            self.path == other.path
            and self.photo_count == other.photo_count
            and self._stats == other._stats
        )

    def __repr__(self) -> str:
        parts = [f"path={self.path!r}", f"photo_count={self.photo_count}"]
        for k, v in self._stats.items():
            parts.append(f"{k}={v!r}")
        return f"PartitionSummary({', '.join(parts)})"


# ---------------------------------------------------------------------------
# Manifests
# ---------------------------------------------------------------------------


@dataclass
class ThumbnailGridLayout:
    """Grid layout metadata for a thumbnail or preview AVIF container.

    Tiles are ordered by photo content_hash (ascending) for stability:
    a photo's tile index only changes if its content changes, not on renames.
    """

    cols: int                   # number of columns in the AVIF grid
    rows: int                   # number of rows in the AVIF grid
    tile_size: int              # short edge in pixels (e.g. 256 or 1440)
    photo_order: list[str]      # content_hashes in row-major tile order


@dataclass
class LeafManifest:
    """Leaf-level manifest containing full per-photo metadata inline."""

    schema_version: int
    partition: str
    photos: list[PhotoEntry] = field(default_factory=list)
    summary: PartitionSummary | None = None
    thumbnails_hash: str | None = None
    previews_hash: str | None = None
    thumbnail_grid: ThumbnailGridLayout | None = None
    preview_grid: ThumbnailGridLayout | None = None
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
    rating: int | None = None  # xmp:Rating (0=unrated, 1-5=stars, -1=rejected)
    width: int | None = None   # pixel width (exif:PixelXDimension / tiff:ImageWidth)
    height: int | None = None  # pixel height (exif:PixelYDimension / tiff:ImageLength)
    tags: list[str] = field(default_factory=list)
    # Unknown XMP attributes and child elements from third-party apps (Lightroom, darktable, …).
    # Keys use Clark notation: "{ns_uri}localname".
    # Values are either plain strings (for simple attributes) or XML-serialized strings (for
    # structured child elements like bags/sequences, identifiable by a leading "<").
    _extra: dict[str, str] = field(default_factory=dict)


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
    for fdef in PHOTO_FIELDS:
        value = entry.searchable.get(fdef.entry_attr)
        if value is None:
            continue
        if fdef.type is FieldType.DATE_RANGE:
            d[fdef.name] = value.isoformat()
        elif fdef.type is FieldType.GPS_BOX:
            d[fdef.name] = list(value)
        elif fdef.type is FieldType.STRING_COLLECTION:
            if value:
                d[fdef.name] = value
        else:
            d[fdef.name] = value
    d.update(entry._extra)
    return d


def _photo_entry_from_dict(d: dict[str, Any]) -> PhotoEntry:
    known_keys = {"filename", "contentHash", "metadataVersion", "xmpVersionToken"}
    searchable: dict[str, Any] = {}
    for fdef in PHOTO_FIELDS:
        known_keys.add(fdef.name)
        raw = d.get(fdef.name)
        if raw is None:
            continue
        if fdef.type is FieldType.DATE_RANGE:
            searchable[fdef.entry_attr] = datetime.fromisoformat(raw)
        elif fdef.type is FieldType.GPS_BOX:
            searchable[fdef.entry_attr] = tuple(raw)
        else:
            searchable[fdef.entry_attr] = raw
    extra = {k: v for k, v in d.items() if k not in known_keys}
    return PhotoEntry(
        filename=d["filename"],
        content_hash=d["contentHash"],
        metadata_version=d.get("metadataVersion", 1),
        xmp_version_token=d.get("xmpVersionToken", ""),
        searchable=searchable,
        _extra=extra,
    )


def _summary_to_dict(s: PartitionSummary) -> dict[str, Any]:
    d: dict[str, Any] = {
        "path": s.path,
        "photoCount": s.photo_count,
    }
    # _stats already mirrors the JSON structure; only datetime and bytes need conversion.
    for name, stat in s._stats.items():
        t = stat.get("type")
        if t == "date_range":
            out: dict[str, Any] = {"type": "date_range"}
            if stat.get("min") is not None:
                out["min"] = stat["min"].isoformat()
            if stat.get("max") is not None:
                out["max"] = stat["max"].isoformat()
            d[name] = out
        elif t == "int_range":
            d[name] = stat
        elif t == "bloom":
            val = stat.get("value")
            if val:
                d[name] = {"type": "bloom", "value": val.hex() if isinstance(val, bytes) else val}
        elif t == "gps_bbox":
            d[name] = stat  # all values are plain floats; pass through as-is
    d.update(s._extra)
    return d


def _summary_from_dict(d: dict[str, Any]) -> PartitionSummary:
    known_keys = {"path", "photoCount", "hashes"}
    stats: dict[str, dict[str, Any]] = {}
    for fd in PHOTO_FIELDS:
        known_keys.add(fd.name)
        stat = d.get(fd.name)
        if not isinstance(stat, dict):
            continue
        if fd.summary_range and fd.type is FieldType.DATE_RANGE:
            stats[fd.name] = {
                "type": "date_range",
                "min": datetime.fromisoformat(stat["min"]) if "min" in stat else None,
                "max": datetime.fromisoformat(stat["max"]) if "max" in stat else None,
            }
        elif fd.summary_range and fd.type is FieldType.INT_RANGE:
            stats[fd.name] = {"type": "int_range", "min": stat.get("min"), "max": stat.get("max")}
        elif fd.summary_bloom_attr:
            hex_val = stat.get("value", "")
            if hex_val:
                stats[fd.name] = {"type": "bloom", "value": bytes.fromhex(hex_val)}
        elif fd.summary_gps_bbox and fd.type is FieldType.GPS_BOX:
            stats[fd.name] = {
                "type": "gps_bbox",
                "minLat": stat.get("minLat"), "maxLat": stat.get("maxLat"),
                "minLon": stat.get("minLon"), "maxLon": stat.get("maxLon"),
            }
    hashes_stat = d.get("hashes")
    if isinstance(hashes_stat, dict) and hashes_stat.get("value"):
        stats["hashes"] = {"type": "bloom", "value": bytes.fromhex(hashes_stat["value"])}
    extra = {k: v for k, v in d.items() if k not in known_keys}
    return PartitionSummary(
        path=d["path"],
        photo_count=d.get("photoCount", 0),
        _stats=stats,
        _extra=extra,
    )


def _grid_layout_to_dict(g: ThumbnailGridLayout) -> dict[str, Any]:
    return {
        "cols": g.cols,
        "rows": g.rows,
        "tileSize": g.tile_size,
        "photoOrder": g.photo_order,
    }


def _grid_layout_from_dict(d: dict[str, Any]) -> ThumbnailGridLayout:
    return ThumbnailGridLayout(
        cols=d["cols"],
        rows=d["rows"],
        tile_size=d["tileSize"],
        photo_order=d.get("photoOrder", []),
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
    if manifest.thumbnail_grid is not None:
        d["thumbnailGrid"] = _grid_layout_to_dict(manifest.thumbnail_grid)
    if manifest.preview_grid is not None:
        d["previewGrid"] = _grid_layout_to_dict(manifest.preview_grid)
    d.update(manifest._extra)
    return d


def deserialize_leaf(d: dict[str, Any]) -> LeafManifest:
    """Deserialize a JSON dict into a LeafManifest, preserving unknown fields."""
    known_keys = {
        "schemaVersion", "partition", "photos", "summary",
        "thumbnailsHash", "previewsHash", "thumbnailGrid", "previewGrid",
    }
    extra = {k: v for k, v in d.items() if k not in known_keys}
    return LeafManifest(
        schema_version=d.get("schemaVersion", SCHEMA_VERSION),
        partition=d["partition"],
        photos=[_photo_entry_from_dict(p) for p in d.get("photos", [])],
        summary=_summary_from_dict(d["summary"]) if d.get("summary") else None,
        thumbnails_hash=d.get("thumbnailsHash"),
        previews_hash=d.get("previewsHash"),
        thumbnail_grid=_grid_layout_from_dict(d["thumbnailGrid"]) if d.get("thumbnailGrid") else None,
        preview_grid=_grid_layout_from_dict(d["previewGrid"]) if d.get("previewGrid") else None,
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
