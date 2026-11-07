"""Query building blocks shared by the index readers: full-text filter and SQL quoting."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FtsFilter:
    """Full-text search filter for TEXT-typed fields.

    query:   Single search string applied across all listed columns.
    columns: Lance column names that carry FTS indexes (e.g. ``["description"]``).
    """

    query: str
    columns: list[str]


def sql_quote(s: str) -> str:
    """Escape single quotes for SQL WHERE clause values."""
    return s.replace("'", "''")
