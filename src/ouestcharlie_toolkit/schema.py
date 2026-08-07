"""Shared data models and constants for the OuEstCharlie toolkit."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ouestcharlie_toolkit.fields import PHOTO_FIELDS, FieldDef, FieldType

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OUESTCHARLIE_NS = "http://ouestcharlie.app/ns/1.0/"
SCHEMA_VERSION = 3
SUMMARY_FILENAME = "summary.json"
METADATA_DIR = ".ouestcharlie"
PREVIEW_JPEG_SUBDIR = "previews"
LANCE_INDEX_SUBDIR = "index.lance"


def summary_path() -> str:
    """Well-known path for the root summary file: '.ouestcharlie/summary.json'."""
    return f"{METADATA_DIR}/{SUMMARY_FILENAME}"


def lance_index_path() -> str:
    """Well-known backend-relative path
    for the LanceDB index directory: '.ouestcharlie/index.lance'."""
    return f"{METADATA_DIR}/{LANCE_INDEX_SUBDIR}"


def preview_jpeg_path(partition: str, content_hash: str) -> str:
    """Backend-relative path for a per-photo JPEG preview cache file.

    Example: ``'2024/2024-07'`` → ``'.ouestcharlie/2024/2024-07/previews/Kf3QzA2nBcR8xYvLP9w.jpg'``.
    Root partition (``''``) → ``'.ouestcharlie/previews/Kf3QzA2nBcR8xYvLm1P9w.jpg'``.
    """
    suffix = partition.rstrip("/") + "/" if partition else ""
    return f"{METADATA_DIR}/{suffix}{PREVIEW_JPEG_SUBDIR}/{content_hash}.jpg"


# ---------------------------------------------------------------------------
# Photo entry
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


class ManifestSummary:
    """Aggregate statistics over a set of photos (count plus per-field ranges).

    Per-field statistics are stored in ``_stats`` as typed dicts that mirror
    the JSON serialisation format:

    - date range:  ``{"type": "date_range", "min": datetime, "max": datetime}``
    - int range:   ``{"type": "int_range",  "min": int,      "max": int}``

    Field stats are accessed via normal attribute syntax (``__getattr__``),
    e.g. ``summary.date["min"]``, ``summary.rating["max"]``.

    Adding a new summarisable field requires only a ``FieldDef`` entry in
    ``fields.py`` — no changes needed here.
    """

    def __init__(
        self,
        photo_count: int = 0,
        _stats: dict[str, dict[str, Any]] | None = None,
        _extra: dict[str, Any] | None = None,
    ) -> None:
        self.photo_count = photo_count
        self._stats: dict[str, dict[str, Any]] = dict(_stats) if _stats else {}
        self._extra: dict[str, Any] = dict(_extra) if _extra is not None else {}

    def __getattr__(self, name: str) -> Any:
        """Return the typed stat dict for a field,
        e.g. summary.rating → {"type": "int_range", ...}."""
        return self.__dict__.get("_stats", {}).get(name)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ManifestSummary):
            return NotImplemented
        return self.photo_count == other.photo_count and self._stats == other._stats

    def __repr__(self) -> str:
        parts = [f"photo_count={self.photo_count}"]
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

    Column count is always 8 — not stored.
    """

    rows: int  # number of rows in the AVIF grid
    tile_size: int  # short edge in pixels (e.g. 256 or 1440)
    photo_order: list[str]  # content_hashes in row-major tile order


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

    avif_hash: str  # 22-char BLAKE3 of the AVIF content
    grid: ThumbnailGridLayout  # rows, tile_size, photo_order


def thumbnail_avif_path(partition: str, avif_hash: str, tier: str = "thumbnail") -> str:
    """Reconstruct the backend-relative path for a thumbnail AVIF chunk.

    Example: thumbnail_avif_path("2024/Jul", "Kf3QzA2_nBcR8xYvLm1P9w")
             → ".ouestcharlie/2024/Jul/thumbnails-Kf3QzA2_nBcR8xYvLm1P9w.avif"
    Root partition (``''``) → ``'.ouestcharlie/thumbnails-{hash}.avif'``.
    """
    suffix = partition.rstrip("/") + "/" if partition else ""
    stem = "thumbnails" if tier == "thumbnail" else "previews"
    return f"{METADATA_DIR}/{suffix}{stem}-{avif_hash}.avif"


@dataclass
class RootSummary:
    """Thin schema-version marker for a backend.

    Written at <backend-root>/.ouestcharlie/summary.json, once per full
    indexing session. Its only purpose is letting readers (Wally) detect an
    unindexed library or a stale schema version without opening the LanceDB
    index. Per-partition and library-wide statistics are computed at query
    time instead (see ``compute_summary`` in ``partition_summary.py``).
    """

    schema_version: int
    last_indexed_at: datetime | None = None
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
    width: int | None = None  # pixel width (exif:PixelXDimension / tiff:ImageWidth)
    height: int | None = None  # pixel height (exif:PixelYDimension / tiff:ImageLength)
    tags: list[str] = field(default_factory=list)
    description: str | None = None  # dc:description (human-readable caption, any language)
    # Camera shoot settings
    iso_speed: int | None = None  # exif:ISOSpeedRatings
    aperture: float | None = None  # exif:FNumber (e.g. 2.8)
    exposure_time: float | None = None  # exif:ExposureTime in seconds (e.g. 0.004 = 1/250)
    focal_length: float | None = None  # exif:FocalLength in mm
    focal_length_35mm: int | None = None  # exif:FocalLengthIn35mmFilm
    lens_model: str | None = None  # aux:Lens / exifEX:LensModel / Exif.Photo.LensModel
    # Video fields. media_type defaults to "photo" so every existing photo sidecar
    # keeps its meaning without a rewrite; the video-only fields stay None for photos.
    media_type: str = "photo"  # "photo" | "video"
    duration_seconds: float | None = None  # container duration in seconds
    video_codec: str | None = None  # video stream codec name (e.g. "h264", "hevc")
    has_audio: bool | None = None  # whether the container carries an audio stream
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


def _summary_to_dict(s: ManifestSummary) -> dict[str, Any]:
    d: dict[str, Any] = {
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
            if stat.get("missing"):
                out["missing"] = stat["missing"]
            d[name] = out
        elif t == "int_range" or t == "tag_facets":
            d[name] = stat
    d.update(s._extra)
    return d


def _summary_from_dict(d: dict[str, Any]) -> ManifestSummary:
    known_keys = {"photoCount", "hashes"}
    stats: dict[str, dict[str, Any]] = {}
    for fd in PHOTO_FIELDS:
        known_keys.add(fd.name)
        stat = d.get(fd.name)
        if not isinstance(stat, dict):
            continue
        if fd.summary_range and fd.type is FieldType.DATE_RANGE:
            parsed: dict[str, Any] = {
                "type": "date_range",
                "min": datetime.fromisoformat(stat["min"]) if "min" in stat else None,
                "max": datetime.fromisoformat(stat["max"]) if "max" in stat else None,
            }
            if stat.get("missing"):
                parsed["missing"] = stat["missing"]
            stats[fd.name] = parsed
        elif fd.summary_range and fd.type is FieldType.INT_RANGE:
            parsed = {
                "type": "int_range",
                "min": stat.get("min"),
                "max": stat.get("max"),
            }
            if stat.get("missing"):
                parsed["missing"] = stat["missing"]
            stats[fd.name] = parsed
    hashes_stat = d.get("hashes")
    if isinstance(hashes_stat, dict) and hashes_stat.get("value"):
        stats["hashes"] = {
            "type": "bloom",
            "value": bytes.fromhex(hashes_stat["value"]),
        }
    extra = {k: v for k, v in d.items() if k not in known_keys}
    return ManifestSummary(
        photo_count=d.get("photoCount", 0),
        _stats=stats,
        _extra=extra,
    )


def serialize_summary(s: RootSummary) -> dict[str, Any]:
    """Serialize a RootSummary to a JSON-compatible dict."""
    d: dict[str, Any] = {"schemaVersion": s.schema_version}
    if s.last_indexed_at is not None:
        d["lastIndexedAt"] = s.last_indexed_at.isoformat()
    d.update(s._extra)
    return d


def deserialize_summary(d: dict[str, Any]) -> RootSummary:
    """Deserialize a JSON dict into a RootSummary, preserving unknown fields.

    Tolerant of the legacy bulky shape (a ``partitions`` list) written before
    this thin-marker redesign — that field is simply ignored, not preserved.
    """
    known_keys = {"schemaVersion", "lastIndexedAt", "partitions"}
    extra = {k: v for k, v in d.items() if k not in known_keys}
    last_indexed_raw = d.get("lastIndexedAt")
    return RootSummary(
        schema_version=d.get("schemaVersion", SCHEMA_VERSION),
        last_indexed_at=datetime.fromisoformat(last_indexed_raw) if last_indexed_raw else None,
        _extra=extra,
    )
