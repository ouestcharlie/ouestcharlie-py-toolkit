"""The LanceDB photo index: table schema, queries, summaries and the summary.json marker."""

from ouestcharlie_toolkit.index.lance_index import PAGE_SIZE, LanceIndex
from ouestcharlie_toolkit.index.partition_summary import compute_summary
from ouestcharlie_toolkit.index.query import FtsFilter, sql_quote
from ouestcharlie_toolkit.index.schema import (
    PHOTO_ENTRY_COLUMNS,
    PHOTO_SCHEMA,
    PHOTO_TABLE_NAME,
    photo_entry_to_row,
    row_to_photo_entry,
)
from ouestcharlie_toolkit.index.summary_store import SummaryStore

__all__ = [
    "PAGE_SIZE",
    "PHOTO_ENTRY_COLUMNS",
    "PHOTO_SCHEMA",
    "PHOTO_TABLE_NAME",
    "FtsFilter",
    "LanceIndex",
    "SummaryStore",
    "compute_summary",
    "photo_entry_to_row",
    "row_to_photo_entry",
    "sql_quote",
]
