"""LanceDB-backed photo index for OuEstCharlie.

Columnar store at .ouestcharlie/index.lance/ inside each backend root.

"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import lancedb
import pyarrow as pa
from lancedb.index import FTS
from lancedb.query import AsyncFTSQuery, AsyncQuery

from ouestcharlie_toolkit.index.migrations import ensure_tag_terms_index, migrate_table
from ouestcharlie_toolkit.index.query import FtsFilter, sql_quote
from ouestcharlie_toolkit.index.schema import PHOTO_SCHEMA, photo_entry_to_row
from ouestcharlie_toolkit.model.entries import PhotoEntry
from ouestcharlie_toolkit.storage.backend import Backend
from ouestcharlie_toolkit.storage.layout import lance_index_path

_log = logging.getLogger(__name__)

DEFAULT_LIMIT = 10_000
PAGE_SIZE = 500


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
            await migrate_table(table)
            if create_if_missing:  # writer (Whitebeard) only — readers never build indexes
                await ensure_tag_terms_index(table)
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
            await ensure_tag_terms_index(table)
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
                .where(f"partition = '{sql_quote(partition)}'")
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

        # Step 2: Build rows, deduplicating within the batch. Callers pick one
        # representative per hash, so a duplicate here is a caller bug: warn,
        # and keep the lexicographically smallest filename so the result is
        # deterministic.
        seen_hashes: set[str] = set()
        rows_to_write = []
        for entry in sorted(entries, key=lambda e: e.filename):
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
        hash_list = ", ".join(f"'{sql_quote(h)}'" for h in content_hashes)
        query = f"partition = '{sql_quote(partition)}' AND content_hash IN ({hash_list})"
        await self._table.delete(query)

    async def delete_partition(self, partition: str) -> None:
        """Delete all rows for a partition."""
        await self._table.delete(f"partition = '{sql_quote(partition)}'")

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
            query = self._table.query().where(f"partition = '{sql_quote(partition)}'").limit(limit)
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

        def _base_query() -> AsyncQuery | AsyncFTSQuery:
            q = self._table.query()
            if where_clause:
                q = q.where(where_clause)
            if fts_filter:
                return q.nearest_to_text(fts_filter.query, columns=fts_filter.columns)
            return q

        # Query 1: total count.
        if fts_filter:
            # nearest_to_text has no native row-count equivalent (table.count_rows
            # only accepts a SQL filter, not an FTS ranking query), so counting
            # means materializing matches — and nearest_to_text applies an
            # implicit default row cap when no .limit() is chained, which would
            # silently truncate this count for any FTS search matching more rows
            # than that default, hence the explicit limit.
            count_table: pa.Table = (
                await _base_query().select(["content_hash"]).limit(DEFAULT_LIMIT).to_arrow()
            )
            total_count = len(count_table)
        else:
            # Plain SQL filter: the table's native count_rows avoids materializing
            # any rows at all.
            total_count = await self._table.count_rows(where_clause)

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
                # Defense-in-depth: callers (Wally) validate order_by against the
                # sortable-field set before reaching here, so a failure now signals
                # a bug rather than bad user input — hence error, not warning.
                _log.error("order_by(%r) failed, returning page unsorted: %s", order_by, exc)
                page_rows = (
                    await _base_query()
                    .order_by([ColumnOrdering(column_name="filename", ascending=True)])
                    .offset(page * page_size)
                    .limit(page_size)
                    .to_list()
                )

        return page_rows, total_count
