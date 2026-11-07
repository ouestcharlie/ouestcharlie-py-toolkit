"""Lance table schema and PhotoEntry <-> row conversion."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pyarrow as pa

from ouestcharlie_toolkit.model.entries import PhotoEntry
from ouestcharlie_toolkit.model.fields import PHOTO_FIELDS, FieldType
from ouestcharlie_toolkit.model.tags import tag_terms

PHOTO_SCHEMA = pa.schema(
    [
        pa.field("content_hash", pa.string()),
        pa.field("filename", pa.string()),
        pa.field("partition", pa.string()),
        pa.field("date_taken", pa.timestamp("us"), nullable=True),
        # UTC instant of date_taken, for cross-timezone chronological ordering.
        # Null when no offset is known (date_taken is then local wall-clock only).
        pa.field("date_taken_utc", pa.timestamp("us"), nullable=True),
        pa.field("utc_offset_minutes", pa.int16(), nullable=True),
        pa.field("rating", pa.int32(), nullable=True),
        pa.field("width", pa.int32(), nullable=True),
        pa.field("height", pa.int32(), nullable=True),
        pa.field("orientation", pa.int32(), nullable=True),
        pa.field("make", pa.string(), nullable=True),
        pa.field("model", pa.string(), nullable=True),
        # Leaf tag paths ("Places|Europe|France"), as read from the sidecar.
        pa.field("tags", pa.list_(pa.string())),
        # Derived from tags: every ancestor path and every level name, for
        # subtree and any-level tag filters (LABEL_LIST-indexed).
        pa.field("tag_terms", pa.list_(pa.string()), nullable=True),
        pa.field("gps_lat", pa.float64(), nullable=True),
        pa.field("gps_lon", pa.float64(), nullable=True),
        # Caption (dc:description) — FTS-indexed
        pa.field("description", pa.string(), nullable=True),
        # Shoot settings
        pa.field("iso_speed", pa.int32(), nullable=True),
        pa.field("aperture", pa.float32(), nullable=True),
        pa.field("exposure_time", pa.float32(), nullable=True),
        pa.field("focal_length", pa.float32(), nullable=True),
        pa.field("focal_length_35mm", pa.int32(), nullable=True),
        pa.field("lens_model", pa.string(), nullable=True),
        # Video fields — null for photos (media_type defaults to "photo").
        pa.field("media_type", pa.string(), nullable=True),
        pa.field("duration_seconds", pa.float64(), nullable=True),
        pa.field("video_codec", pa.string(), nullable=True),
        pa.field("has_audio", pa.bool_(), nullable=True),
        pa.field("metadata_version", pa.int64()),
        pa.field("xmp_version_token", pa.string()),
        # Thumbnail tile location — flat nullable columns to avoid null-struct ambiguity.
        pa.field("thumbnail_avif_hash", pa.string(), nullable=True),  # NULL until thumbnail built
        pa.field("thumbnail_tile_index", pa.int16(), nullable=True),
        pa.field("_last_update", pa.timestamp("us")),
    ]
)

PHOTO_TABLE_NAME = "photos"
DEFAULT_LIMIT = 10_000


def photo_entry_to_row(
    entry: PhotoEntry,
    partition: str,
    thumbnail: tuple[str, int] | None,
) -> dict[str, Any]:
    """Convert a PhotoEntry to a LanceDB row dict.

    thumbnail: (avif_hash, tile_index) or None.
    """
    s = entry.searchable
    gps = s.get("gps")
    dt = s.get("date_taken")
    # date_taken is stored as naive local wall-clock (for calendar queries). When the
    # source carries a UTC offset (EXIF OffsetTimeOriginal, or a resolved video offset),
    # dt is timezone-aware: derive the UTC instant and the signed offset before stripping
    # tzinfo. Without an offset, dt is naive and both UTC columns stay null.
    offset = dt.utcoffset() if dt is not None else None
    date_taken_utc = (
        dt.astimezone(UTC).replace(tzinfo=None) if dt is not None and offset is not None else None
    )
    return {
        "content_hash": entry.content_hash,
        "filename": entry.filename,
        "partition": partition,
        "date_taken": dt.replace(tzinfo=None) if dt is not None else None,
        "date_taken_utc": date_taken_utc,
        "utc_offset_minutes": (int(offset.total_seconds() / 60) if offset is not None else None),
        "rating": s.get("rating"),
        "width": s.get("width"),
        "height": s.get("height"),
        "orientation": s.get("orientation"),
        "make": s.get("make"),
        "model": s.get("model"),
        "tags": list(s.get("tags") or []),
        "tag_terms": tag_terms(s.get("tags") or []),
        "gps_lat": gps[0] if gps is not None else None,
        "gps_lon": gps[1] if gps is not None else None,
        "description": s.get("description"),
        "iso_speed": s.get("iso_speed"),
        "aperture": s.get("aperture"),
        "exposure_time": s.get("exposure_time"),
        "focal_length": s.get("focal_length"),
        "focal_length_35mm": s.get("focal_length_35mm"),
        "lens_model": s.get("lens_model"),
        "media_type": s.get("media_type"),
        "duration_seconds": s.get("duration_seconds"),
        "video_codec": s.get("video_codec"),
        "has_audio": s.get("has_audio"),
        "metadata_version": entry.metadata_version,
        "xmp_version_token": entry.xmp_version_token,
        "thumbnail_avif_hash": thumbnail[0] if thumbnail is not None else None,
        "thumbnail_tile_index": thumbnail[1] if thumbnail is not None else None,
        "_last_update": datetime.now(UTC).replace(tzinfo=None),
    }


# Columns required by row_to_photo_entry — pass to get_partition_rows(columns=…)
# to avoid fetching thumbnail, partition, and bookkeeping columns.
PHOTO_ENTRY_COLUMNS: list[str] = [
    "filename",
    "content_hash",
    "date_taken",
    "rating",
    "width",
    "height",
    "orientation",
    "make",
    "model",
    "tags",
    "gps_lat",
    "gps_lon",
    "description",
    "iso_speed",
    "aperture",
    "exposure_time",
    "focal_length",
    "focal_length_35mm",
    "lens_model",
    "media_type",
    "duration_seconds",
    "video_codec",
    "has_audio",
    "metadata_version",
    "xmp_version_token",
]


def row_to_photo_entry(row: dict[str, Any]) -> PhotoEntry:
    """Reconstruct a PhotoEntry from a LanceDB row dict."""
    searchable: dict[str, Any] = {}
    for fdef in PHOTO_FIELDS:
        if fdef.type is FieldType.GPS_BOX:
            lat = row.get("gps_lat")
            lon = row.get("gps_lon")
            searchable[fdef.entry_attr] = (lat, lon) if lat is not None or lon is not None else None
        elif fdef.type is FieldType.DATE_RANGE:
            searchable[fdef.entry_attr] = row.get("date_taken")
        elif fdef.type is FieldType.STRING_COLLECTION:
            searchable[fdef.entry_attr] = list(row.get("tags") or [])
        else:
            searchable[fdef.entry_attr] = row.get(fdef.entry_attr)
    return PhotoEntry(
        filename=row["filename"],
        content_hash=row["content_hash"],
        searchable=searchable,
        metadata_version=int(row.get("metadata_version") or 1),
        xmp_version_token=str(row.get("xmp_version_token") or ""),
    )
