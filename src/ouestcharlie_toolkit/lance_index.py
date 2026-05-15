"""LanceDB-backed photo index for OuEstCharlie.

Columnar store at .ouestcharlie/index.lance/ inside each backend root.

"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

import lancedb
import pyarrow as pa

from .backend import Backend
from .fields import PHOTO_FIELDS, FieldType
from .schema import PhotoEntry, lance_index_path

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

PHOTO_SCHEMA = pa.schema(
    [
        pa.field("content_hash", pa.string()),
        pa.field("filename", pa.string()),
        pa.field("partition", pa.string()),
        pa.field("date_taken", pa.timestamp("us"), nullable=True),
        pa.field("utc_offset_minutes", pa.int16(), nullable=True),
        pa.field("rating", pa.int32(), nullable=True),
        pa.field("width", pa.int32(), nullable=True),
        pa.field("height", pa.int32(), nullable=True),
        pa.field("orientation", pa.int32(), nullable=True),
        pa.field("make", pa.string(), nullable=True),
        pa.field("model", pa.string(), nullable=True),
        pa.field("tags", pa.list_(pa.string())),
        pa.field("gps_lat", pa.float64(), nullable=True),
        pa.field("gps_lon", pa.float64(), nullable=True),
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


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------


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
    return {
        "content_hash": entry.content_hash,
        "filename": entry.filename,
        "partition": partition,
        "date_taken": dt.replace(tzinfo=None) if dt is not None else None,
        "utc_offset_minutes": None,  # not yet extracted
        "rating": s.get("rating"),
        "width": s.get("width"),
        "height": s.get("height"),
        "orientation": s.get("orientation"),
        "make": s.get("make"),
        "model": s.get("model"),
        "tags": list(s.get("tags") or []),
        "gps_lat": gps[0] if gps is not None else None,
        "gps_lon": gps[1] if gps is not None else None,
        "metadata_version": entry.metadata_version,
        "xmp_version_token": entry.xmp_version_token,
        "thumbnail_avif_hash": thumbnail[0] if thumbnail is not None else None,
        "thumbnail_tile_index": thumbnail[1] if thumbnail is not None else None,
        "_last_update": datetime.now(UTC).replace(tzinfo=None),
    }


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


# ---------------------------------------------------------------------------
# LanceIndex
# ---------------------------------------------------------------------------


def _esc(s: str) -> str:
    """Escape single quotes for SQL WHERE clause values."""
    return s.replace("'", "''")


class LanceIndex:
    """Thin wrapper around a LanceDB table storing all photos for one backend."""

    def __init__(self, table: lancedb.table.Table) -> None:
        self._table = table

    @classmethod
    async def open_or_create(cls, backend: Backend, table_name: str) -> LanceIndex:
        """Open an existing LanceDB index, or create one if absent."""
        uri = str(await backend.local_path(lance_index_path()))

        def _lance_worker() -> lancedb.table.Table:
            db = lancedb.connect(uri)
            return db.create_table(table_name, schema=PHOTO_SCHEMA, exist_ok=True)

        return cls(await asyncio.to_thread(_lance_worker))

    @classmethod
    async def open(cls, backend: Backend, table_name: str) -> LanceIndex:
        """Open an existing LanceDB index (raises FileNotFoundError if absent)."""
        uri = str(await backend.local_path(lance_index_path()))

        def _lance_worker() -> lancedb.table.Table:
            db = lancedb.connect(uri)
            if table_name not in db.list_tables().tables:
                raise FileNotFoundError(f"LanceDB index not found at {uri!r}")
            return db.open_table(table_name)

        return cls(await asyncio.to_thread(_lance_worker))

    # -----------------------------------------------------------------------
    # Write operations
    # -----------------------------------------------------------------------

    async def upsert_partition(
        self,
        partition: str,
        entries: list[PhotoEntry],
        thumbnail_lookup: dict[str, tuple[str, int]] | None = None,
    ) -> None:
        """Upsert all photo rows for a partition.

        thumbnail_lookup: content_hash → (avif_hash, tile_index) for photos
        with newly generated thumbnails.  For photos absent from this map,
        existing thumbnail data is preserved by querying the table first.
        """
        if not entries:
            return

        # Retrieve existing thumbnail data so incremental upserts don't wipe it.
        def _lance_worker():
            existing_thumbs: dict[str, tuple[str, int] | None] = {}
            try:
                rows = (
                    self._table.search()
                    .where(f"partition = '{_esc(partition)}'", prefilter=True)
                    .select(["content_hash", "thumbnail_avif_hash", "thumbnail_tile_index"])
                    .limit(len(entries) + 1000)
                    .to_list()
                )
                for r in rows:
                    avif = r.get("thumbnail_avif_hash")
                    idx = r.get("thumbnail_tile_index")
                    existing_thumbs[r["content_hash"]] = (avif, idx) if avif is not None else None
            except Exception as exc:
                _log.debug("Could not fetch existing thumbnails for %r: %s", partition, exc)

            rows_to_write = []
            for entry in entries:
                if thumbnail_lookup and entry.content_hash in thumbnail_lookup:
                    thumb: tuple[str, int] | None = thumbnail_lookup[entry.content_hash]
                else:
                    thumb = existing_thumbs.get(entry.content_hash)
                rows_to_write.append(photo_entry_to_row(entry, partition, thumb))

            table_data = pa.Table.from_pylist(rows_to_write, schema=PHOTO_SCHEMA)
            (
                self._table.merge_insert("content_hash")
                .when_matched_update_all()
                .when_not_matched_insert_all()
                .execute(table_data)
            )

        await asyncio.to_thread(_lance_worker)

    async def delete_photos(self, content_hashes: list[str]) -> None:
        """Delete specific photos by content hash."""
        if not content_hashes:
            return
        hash_list = ", ".join(f"'{_esc(h)}'" for h in content_hashes)
        await asyncio.to_thread(self._table.delete, f"content_hash IN ({hash_list})")

    async def delete_partition(self, partition: str) -> None:
        """Delete all rows for a partition."""
        await asyncio.to_thread(self._table.delete, f"partition = '{_esc(partition)}'")

    # -----------------------------------------------------------------------
    # Read operations
    # -----------------------------------------------------------------------

    async def get_partition_rows(
        self, partition: str, limit: int = DEFAULT_LIMIT
    ) -> list[dict[str, Any]]:
        """Return all rows for a partition"""
        try:

            def _lance_worker():
                return (
                    self._table.search()
                    .where(f"partition = '{_esc(partition)}'", prefilter=True)
                    .limit(limit)
                    .to_list()
                )

            return await asyncio.to_thread(_lance_worker)
        except Exception:
            return []

    async def search_where(
        self,
        where_clause: str | None,
        root: str = "",
        limit: int = DEFAULT_LIMIT,
    ) -> list[dict[str, Any]]:
        """Execute a filtered search and return matching rows.

        Args:
            where_clause: SQL WHERE expression (without the WHERE keyword).
                None means no filter (all photos).
            root: Restrict to this partition prefix (empty = all partitions).
            limit: Maximum number of results to return.
        """
        clauses: list[str] = []
        if root:
            root_esc = _esc(root.rstrip("/"))
            clauses.append(f"(partition = '{root_esc}' OR starts_with(partition, '{root_esc}/'))")
        if where_clause:
            clauses.append(where_clause)

        combined = " AND ".join(clauses) if clauses else None

        def _lance_worker():
            query = self._table.search().limit(limit)
            if combined:
                query = query.where(combined, prefilter=True)
            return query.to_list()

        return await asyncio.to_thread(_lance_worker)
