"""Shared data models, exceptions, and constants for the OuEstCharlie toolkit."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ouestcharlie_toolkit.fields import PHOTO_FIELDS, FieldDef, FieldType


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OUESTCHARLIE_NS = "http://ouestcharlie.app/ns/1.0/"
SCHEMA_VERSION = 1
MANIFEST_FILENAME = "manifest.json"
SUMMARY_FILENAME = "summary.json"
METADATA_DIR = ".ouestcharlie"


def manifest_path(partition: str) -> str:
    """Well-known manifest path for a partition, e.g. '2024/2024-07/' -> '2024/2024-07/.ouestcharlie/manifest.json'."""
    prefix = partition.rstrip("/") + "/" if partition else ""
    return f"{prefix}{METADATA_DIR}/{MANIFEST_FILENAME}"


def summary_path() -> str:
    """Well-known path for the root summary file: '.ouestcharlie/summary.json'."""
    return f"{METADATA_DIR}/{SUMMARY_FILENAME}"


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

    @classmethod
    def from_sidecar(
        cls,
        filename: str,
        sidecar: XmpSidecar,
        content_hash: str,
        xmp_version_token: str,
        field_config: list[FieldDef] | None = None,
    ) -> PhotoEntry:
        """Build a PhotoEntry from an XmpSidecar."""
        if field_config is None:
            field_config = PHOTO_FIELDS
        searchable: dict[str, Any] = {}
        for fdef in field_config:
            if fdef.sidecar_attr is not None:
                val = getattr(sidecar, fdef.sidecar_attr, None)
                if fdef.type is FieldType.STRING_COLLECTION and val is not None:
                    val = list(val)  # defensive copy
                searchable[fdef.entry_attr] = val
        return cls(
            filename=filename,
            content_hash=content_hash,
            metadata_version=sidecar.metadata_version,
            xmp_version_token=xmp_version_token,
            searchable=searchable,
        )


# ---------------------------------------------------------------------------
# Manifest summary
# ---------------------------------------------------------------------------


def _naive(dt: datetime) -> datetime:
    """Return a timezone-naive datetime for ordering.

    Strips tzinfo so that min()/max() can compare a mix of aware and naive
    datetimes without raising TypeError.
    """
    return dt.replace(tzinfo=None)


class ManifestSummary:
    """Summary statistics for a partition, stored inline in manifest.json and
    as an entry in the root summary.json.

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

    @classmethod
    def from_photos(
        cls,
        partition: str,
        entries: list[PhotoEntry],
        field_config: list[FieldDef] | None = None,
    ) -> ManifestSummary:
        """Compute partition-level summary statistics from photo entries."""
        if field_config is None:
            field_config = PHOTO_FIELDS
        stats: dict[str, Any] = {}
        for fdef in field_config:
            if fdef.summary_range:
                values = [v for e in entries if (v := e.searchable.get(fdef.entry_attr)) is not None]
                if not values:
                    continue
                if fdef.type == FieldType.DATE_RANGE:
                    stats[fdef.name] = {
                        "type": "date_range",
                        "min": min(values, key=_naive),
                        "max": max(values, key=_naive),
                    }
                elif fdef.type == FieldType.INT_RANGE:
                    stats[fdef.name] = {"type": "int_range", "min": min(values), "max": max(values)}
            elif fdef.summary_gps_bbox and fdef.type is FieldType.GPS_BOX:
                values = [v for e in entries if (v := e.searchable.get(fdef.entry_attr)) is not None]
                if values:
                    lats = [v[0] for v in values]
                    lons = [v[1] for v in values]
                    stats[fdef.name] = {
                        "type": "gps_bbox",
                        "minLat": min(lats), "maxLat": max(lats),
                        "minLon": min(lons), "maxLon": max(lons),
                    }
        return cls(path=partition, photo_count=len(entries), _stats=stats)

    def __getattr__(self, name: str) -> Any:
        """Return the typed stat dict for a field, e.g. summary.rating → {"type": "int_range", ...}."""
        return self.__dict__.get("_stats", {}).get(name)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ManifestSummary):
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
        return f"ManifestSummary({', '.join(parts)})"


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
class ThumbnailChunk:
    """One AVIF grid file for a subset of photos in a partition.

    A partition's thumbnails are split into chunks of at most GRID_MAX_PHOTOS
    (64) photos each, producing a max 8×8 grid per file.  Each chunk is
    identified by its content hash, which is used as part of its filename
    (``thumbnails-{avif_hash}.avif``).

    The backend path is not stored — reconstruct it with
    ``thumbnail_avif_path(partition, chunk.avif_hash)``.
    """

    avif_hash: str             # 22-char BLAKE3 of the AVIF content
    grid: ThumbnailGridLayout  # cols, rows, tile_size, photo_order


def thumbnail_avif_path(partition: str, avif_hash: str, tier: str = "thumbnail") -> str:
    """Reconstruct the backend-relative path for a thumbnail AVIF chunk.

    Example: thumbnail_avif_path("2024/Jul", "Kf3QzA2_nBcR8xYvLm1P9w")
             → "2024/Jul/.ouestcharlie/thumbnails-Kf3QzA2_nBcR8xYvLm1P9w.avif"
    """
    prefix = partition.rstrip("/") + "/" if partition else ""
    stem = "thumbnails" if tier == "thumbnail" else "previews"
    return f"{prefix}{METADATA_DIR}/{stem}-{avif_hash}.avif"


@dataclass
class LeafManifest:
    """Leaf-level manifest containing full per-photo metadata inline."""

    schema_version: int
    partition: str
    photos: list[PhotoEntry] = field(default_factory=list)
    summary: ManifestSummary | None = None
    thumbnail_chunks: list[ThumbnailChunk] = field(default_factory=list)
    _extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class RootSummary:
    """Flat index of all partition summaries for a backend.

    Written at <backend-root>/.ouestcharlie/summary.json.
    Any folder that directly contains photos gets a manifest.json, and
    summary.json at the root holds a flat list of all such partitions for
    pruning during search.
    """

    schema_version: int
    partitions: list[ManifestSummary] = field(default_factory=list)
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


def _summary_to_dict(s: ManifestSummary) -> dict[str, Any]:
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


def _summary_from_dict(d: dict[str, Any]) -> ManifestSummary:
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
    return ManifestSummary(
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


def _thumbnail_chunk_to_dict(c: ThumbnailChunk) -> dict[str, Any]:
    return {
        "avifHash": c.avif_hash,
        "grid": _grid_layout_to_dict(c.grid),
    }


def _thumbnail_chunk_from_dict(d: dict[str, Any]) -> ThumbnailChunk:
    return ThumbnailChunk(
        avif_hash=d["avifHash"],
        grid=_grid_layout_from_dict(d["grid"]),
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
    if manifest.thumbnail_chunks:
        d["thumbnailChunks"] = [_thumbnail_chunk_to_dict(c) for c in manifest.thumbnail_chunks]
    d.update(manifest._extra)
    return d


def deserialize_leaf(d: dict[str, Any]) -> LeafManifest:
    """Deserialize a JSON dict into a LeafManifest, preserving unknown fields."""
    known_keys = {"schemaVersion", "partition", "photos", "summary", "thumbnailChunks"}
    extra = {k: v for k, v in d.items() if k not in known_keys}
    return LeafManifest(
        schema_version=d.get("schemaVersion", SCHEMA_VERSION),
        partition=d["partition"],
        photos=[_photo_entry_from_dict(p) for p in d.get("photos", [])],
        summary=_summary_from_dict(d["summary"]) if d.get("summary") else None,
        thumbnail_chunks=[_thumbnail_chunk_from_dict(c) for c in d.get("thumbnailChunks", [])],
        _extra=extra,
    )


def serialize_summary(s: RootSummary) -> dict[str, Any]:
    """Serialize a RootSummary to a JSON-compatible dict."""
    d: dict[str, Any] = {
        "schemaVersion": s.schema_version,
        "partitions": [_summary_to_dict(p) for p in s.partitions],
    }
    d.update(s._extra)
    return d


def deserialize_summary(d: dict[str, Any]) -> RootSummary:
    """Deserialize a JSON dict into a RootSummary, preserving unknown fields."""
    known_keys = {"schemaVersion", "partitions"}
    extra = {k: v for k, v in d.items() if k not in known_keys}
    return RootSummary(
        schema_version=d.get("schemaVersion", SCHEMA_VERSION),
        partitions=[_summary_from_dict(p) for p in d.get("partitions", [])],
        _extra=extra,
    )
