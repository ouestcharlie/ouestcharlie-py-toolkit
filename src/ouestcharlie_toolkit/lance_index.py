"""LanceDB-backed photo index for OuEstCharlie.

Columnar store at .ouestcharlie/index.lance/ inside each backend root.

"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import lancedb
import pyarrow as pa
from lancedb.index import FTS

from .backend import Backend
from .fields import PHOTO_FIELDS, FieldType
from .schema import PhotoEntry, lance_index_path

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Full Text Filter
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FtsFilter:
    """Full-text search filter for TEXT-typed fields.

    query:   Single search string applied across all listed columns.
    columns: Lance column names that carry FTS indexes (e.g. ``["description"]``).
    """

    query: str
    columns: list[str]


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
PAGE_SIZE = 500


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


# ---------------------------------------------------------------------------
# LanceIndex
# ---------------------------------------------------------------------------


def _esc(s: str) -> str:
    """Escape single quotes for SQL WHERE clause values."""
    return s.replace("'", "''")


async def _migrate_table(table: lancedb.table.AsyncTable) -> None:
    """Add any columns present in PHOTO_SCHEMA that are missing from the on-disk table."""
    try:
        disk_schema = await table.schema()
        existing = set(disk_schema.names)
    except Exception as exc:
        _log.debug("Lance migration: could not read schema: %s", exc)
        return
    for field in PHOTO_SCHEMA:
        if field.name not in existing:
            try:
                await table.add_columns(pa.schema([field]))
                _log.info("Lance migration: added column %r", field.name)
            except Exception as exc:
                _log.warning("Lance migration: could not add column %r: %s", field.name, exc)


class LanceIndex:
    """Thin wrapper around a LanceDB async table storing all photos for one backend."""

    def __init__(self, table: lancedb.table.AsyncTable) -> None:
        self._table = table

    @classmethod
    async def open(
        cls,
        backend: Backend,
        table_name: str,
        *,
        create_if_missing: bool = False,
        index_path: Path | None = None,
    ) -> LanceIndex:
        """Open a LanceDB index, optionally creating it if absent.

        Args:
            backend: Backend providing the default index location.
            table_name: Name of the LanceDB table to open or create.
            create_if_missing: When True, create the index if it does not exist.
                Existing tables are opened without passing a schema to avoid
                version-dependent schema validation before migration runs.
                New tables are created with the full PHOTO_SCHEMA.
            index_path: Override the default index path
                (``backend.local_path(lance_index_path())``).
                Used when the library root is a UNC path where object_store is unreliable.

        Raises:
            FileNotFoundError: If the index is absent and ``create_if_missing`` is False.
        """
        if index_path is not None:
            uri = str(index_path)
        else:
            uri = str(await backend.local_path(lance_index_path()))
        db = await lancedb.connect_async(uri)
        if table_name in (await db.list_tables()).tables:
            table = await db.open_table(table_name)
            await _migrate_table(table)
        elif create_if_missing:
            table = await db.create_table(table_name, schema=PHOTO_SCHEMA)
            # Create FTS index on description — only when the column has a real string type.
            # A Null-typed column (legacy migration artifact) can't be FTS-indexed; it will
            # be fixed automatically on the next full re-index when real data is upserted.
            try:
                schema = await table.schema()
                desc_type = (
                    schema.field("description").type if "description" in schema.names else None
                )
                if desc_type is not None and desc_type != pa.null():
                    await table.create_index("description", config=FTS(), replace=True)
                else:
                    _log.debug("FTS index skipped: description column type is %s", desc_type)
            except Exception as exc:
                _log.debug("FTS index creation skipped: %s", exc)
        else:
            raise FileNotFoundError(f"LanceDB index not found at {uri!r}")
        return cls(table)

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

        # Step 1: Retrieve existing thumbnail data so incremental upserts don't wipe it.
        existing_thumbs: dict[str, tuple[str, int] | None] = {}
        try:
            rows = await (
                self._table.query()
                .where(f"partition = '{_esc(partition)}'")
                .select(["content_hash", "thumbnail_avif_hash", "thumbnail_tile_index"])
                .limit(len(entries) + 1000)
                .to_list()
            )
            for r in rows:
                avif = r.get("thumbnail_avif_hash")
                idx = r.get("thumbnail_tile_index")
                existing_thumbs[r["content_hash"]] = (
                    (str(avif), int(idx)) if avif is not None and idx is not None else None
                )
        except Exception as exc:
            _log.debug("Could not fetch existing thumbnails for %r: %s", partition, exc)

        # Step 2: Build rows, deduplicating within the batch.
        seen_hashes: set[str] = set()
        rows_to_write = []
        for entry in entries:
            if entry.content_hash in seen_hashes:
                _log.warning(
                    "Duplicate content_hash %r in partition %r — skipping %r",
                    entry.content_hash,
                    partition,
                    entry.filename,
                )
                continue
            seen_hashes.add(entry.content_hash)
            if thumbnail_lookup and entry.content_hash in thumbnail_lookup:
                thumb: tuple[str, int] | None = thumbnail_lookup[entry.content_hash]
            else:
                thumb = existing_thumbs.get(entry.content_hash)
            rows_to_write.append(photo_entry_to_row(entry, partition, thumb))

        if not rows_to_write:
            return

        # Step 3: merge_insert.execute() returns AsyncTable._do_merge coroutine — await it.
        table_data = pa.Table.from_pylist(rows_to_write, schema=PHOTO_SCHEMA)
        await (
            self._table.merge_insert(["partition", "content_hash"])
            .when_matched_update_all()
            .when_not_matched_insert_all()
            .execute(table_data)
        )

    async def delete(self, partition: str, content_hashes: list[str]) -> None:
        """Delete specific photos by content hash."""
        if not content_hashes:
            return
        hash_list = ", ".join(f"'{_esc(h)}'" for h in content_hashes)
        query = f"partition = '{_esc(partition)}' AND content_hash IN ({hash_list})"
        await self._table.delete(query)

    async def delete_partition(self, partition: str) -> None:
        """Delete all rows for a partition."""
        await self._table.delete(f"partition = '{_esc(partition)}'")

    async def list_partitions(self) -> set[str]:
        """Return the set of distinct partition values currently in the index.

        Used to detect stale partitions (indexed previously, no longer on
        disk) now that ``summary.json`` no longer carries a partitions list.
        """
        tbl = await self._table.query().select(["partition"]).to_arrow()
        return set(tbl.column("partition").to_pylist())

    async def maintain(self) -> None:
        """Compact fragment files and prune version history older than 1 hour."""
        await self._table.optimize(cleanup_older_than=timedelta(hours=1))
        _log.info("Lance optimize: compaction and version pruning done")

    # -----------------------------------------------------------------------
    # Read operations
    # -----------------------------------------------------------------------

    async def get_partition_rows(
        self,
        partition: str,
        columns: list[str] | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield rows for a partition, streaming via LanceDB RecordBatch chunks.

        columns: restrict to these column names (pass PHOTO_ENTRY_COLUMNS to
            skip thumbnail, partition, and bookkeeping columns).
            None returns all columns.

        Usage::

            async for row in lance_index.get_partition_rows(partition, columns=[…]):
                …
        """
        try:
            query = self._table.query().where(f"partition = '{_esc(partition)}'").limit(limit)
            if columns is not None:
                query = query.select(columns)
            reader = await query.to_batches()
            async for batch in reader:
                for row in batch.to_pylist():
                    yield row
        except Exception as exc:
            _log.debug("get_partition_rows(%r) failed: %s", partition, exc)

    async def search_where(
        self,
        where_clause: str | None,
        fts_filter: FtsFilter | None = None,
        order_by: str = "date_taken",
        order_desc: bool = True,
        page: int = 0,
        page_size: int = PAGE_SIZE,
    ) -> tuple[list[dict[str, Any]], int]:
        """Execute a filtered, sorted, paginated search and return matching rows.

        Uses two queries:
        1. A lightweight scan (single column) for the total matching count.
        2. A page query: FTS via ``nearest_to_text`` when fts_filter is set
           (results ranked by relevance, rows carry ``_score``), otherwise a
           native ORDER BY / OFFSET / LIMIT query.

        Partition/directory scoping is expressed as part of ``where_clause``
        (generated from the ``directory`` field in the search predicate).

        Args:
            where_clause: SQL WHERE expression (without the WHERE keyword).
                None means no filter (all photos).
            fts_filter: Full-text search filter. When set, the page query uses
                ``nearest_to_text`` ranked by relevance; ``order_by`` is ignored.
            order_by: Column to sort by (default "date_taken"). Ignored when
                fts_filter is set.
            order_desc: Sort descending when True (default).
            page: 0-indexed page number.
            page_size: Number of rows per page (default PAGE_SIZE = 500).

        Returns:
            Tuple of (page_rows, total_matching_count).
        """
        from lancedb.query import ColumnOrdering

        def _base_query():
            q = self._table.query()
            if where_clause:
                q = q.where(where_clause)
            if fts_filter:
                q = q.nearest_to_text(fts_filter.query, columns=fts_filter.columns)
            return q

        # Query 1: lightweight scan for the total count (single narrow column).
        count_table: pa.Table = await _base_query().select(["content_hash"]).to_arrow()
        total_count = len(count_table)

        # Query 2: FTS (no sort) or native sort + offset + limit for the page rows.
        if fts_filter:
            page_rows = await _base_query().offset(page * page_size).limit(page_size).to_list()
        else:
            # filename is a stable tiebreaker for deterministic pagination.
            ordering = [
                ColumnOrdering(column_name=order_by, ascending=not order_desc),
                ColumnOrdering(column_name="filename", ascending=True),
            ]
            try:
                page_rows = (
                    await _base_query()
                    .order_by(ordering)
                    .offset(page * page_size)
                    .limit(page_size)
                    .to_list()
                )
            except Exception as exc:
                _log.warning("order_by(%r) failed, returning page unsorted: %s", order_by, exc)
                page_rows = (
                    await _base_query()
                    .order_by([ColumnOrdering(column_name="filename", ascending=True)])
                    .offset(page * page_size)
                    .limit(page_size)
                    .to_list()
                )

        return page_rows, total_count
