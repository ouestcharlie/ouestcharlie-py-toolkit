"""In-place upgrades of an existing Lance table: new columns, missing indexes."""

from __future__ import annotations

import logging

import lancedb
import pyarrow as pa
from lancedb.index import LabelList

from ouestcharlie_toolkit.index.schema import PHOTO_SCHEMA

_log = logging.getLogger(__name__)


async def migrate_table(table: lancedb.table.AsyncTable) -> None:
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


async def ensure_tag_terms_index(table: lancedb.table.AsyncTable) -> None:
    """Create the LABEL_LIST index backing array_has(tag_terms, …) filters if absent.

    Rows written after the index was built are still matched (scanned) until
    ``maintain()`` optimizes the table, so this only affects speed.
    """
    try:
        if any(idx.columns == ["tag_terms"] for idx in await table.list_indices()):
            return
        await table.create_index("tag_terms", config=LabelList(), replace=True)
    except Exception as exc:
        _log.debug("tag_terms index creation skipped: %s", exc)
