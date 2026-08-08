"""Compute summary statistics directly from the Lance index."""

from __future__ import annotations

import asyncio
from typing import Any

import duckdb

from .fields import PHOTO_FIELDS, FieldType
from .lance_index import FtsFilter, LanceIndex
from .schema import ManifestSummary

# Range field types that contribute MIN/MAX summary stats (gated by summary_range),
# mapped to their emitted stat type.
_RANGE_STAT_TYPE = {
    FieldType.DATE_RANGE: "date_range",
    FieldType.INT_RANGE: "int_range",
    FieldType.FLOAT_RANGE: "float_range",
}


def _range_fields() -> list[Any]:
    return [f for f in PHOTO_FIELDS if f.summary_range and f.type in _RANGE_STAT_TYPE]


def _facet_fields() -> list[Any]:
    return [f for f in PHOTO_FIELDS if f.summary_facet and f.type is FieldType.STRING_MATCH]


def _bool_fields() -> list[Any]:
    return [f for f in PHOTO_FIELDS if f.type is FieldType.BOOL]


_TAG_FACETS_SQL = """
SELECT tag, COUNT(*) AS cnt
FROM (SELECT UNNEST(tags) AS tag FROM photos)
GROUP BY tag
ORDER BY cnt DESC
"""


async def compute_summary(
    lance_index: LanceIndex,
    where_clause: str | None,
    fts_filter: FtsFilter | None = None,
) -> ManifestSummary:
    """Compute a ManifestSummary-shaped aggregate over any WHERE/FTS-filtered subset.

    LanceDB handles the row filter, full-text ranking, and column projection;
    DuckDB computes the aggregates. Which stats are emitted is driven entirely by
    ``PHOTO_FIELDS``: range fields (``summary_range``) yield min/max, categorical
    fields (``summary_facet``) and tags yield value-count facets, and BOOL fields
    yield true/false counts. ``where_clause=None`` with no ``fts_filter`` aggregates
    the whole table.
    """
    range_fields = _range_fields()
    facet_fields = _facet_fields()
    bool_fields = _bool_fields()

    # Columns pulled from LanceDB — deduped, order-preserving.
    select_cols: list[str] = []
    for col in (
        [f.entry_attr for f in range_fields]
        + ["gps_lat", "gps_lon", "tags"]
        + [f.entry_attr for f in facet_fields]
        + [f.entry_attr for f in bool_fields]
    ):
        if col not in select_cols:
            select_cols.append(col)

    # No .limit(): this must aggregate over the entire filtered set (matching
    # search_where's unlimited facet-count query), not just one page/partition.
    query = lance_index._table.query()
    if where_clause:
        query = query.where(where_clause)
    if fts_filter:
        query = query.nearest_to_text(fts_filter.query, columns=fts_filter.columns)
    arrow_tbl = await query.select(select_cols).to_arrow()

    # Build the MIN/MAX/COUNT aggregate query dynamically from the range fields.
    agg_parts = ["COUNT(*) AS n"]
    for f in range_fields:
        c = f.entry_attr
        agg_parts += [f"MIN({c}) AS {c}_min", f"MAX({c}) AS {c}_max", f"COUNT({c}) AS {c}_cnt"]
    agg_parts += [
        "MIN(gps_lat) AS lat_min",
        "MAX(gps_lat) AS lat_max",
        "COUNT(gps_lat) AS lat_cnt",
        "MIN(gps_lon) AS lon_min",
        "MAX(gps_lon) AS lon_max",
        "COUNT(gps_lon) AS lon_cnt",
    ]
    agg_sql = "SELECT " + ", ".join(agg_parts) + " FROM photos"

    # DuckDB aggregation is CPU-bound sync — run in a thread pool.
    def _agg() -> tuple[dict[str, Any], list[tuple[str, int]], dict[str, list], dict[str, dict]]:
        conn = duckdb.connect()
        conn.register("photos", arrow_tbl)
        cur = conn.execute(agg_sql)
        cols = [d[0] for d in cur.description]
        agg = dict(zip(cols, cur.fetchone(), strict=True))
        tags = conn.execute(_TAG_FACETS_SQL).fetchall()
        facets: dict[str, list] = {}
        for f in facet_fields:
            c = f.entry_attr
            facets[f.name] = conn.execute(
                f"SELECT {c} AS v, COUNT(*) AS c FROM photos "
                f"WHERE {c} IS NOT NULL GROUP BY {c} ORDER BY c DESC"
            ).fetchall()
        bools: dict[str, dict] = {}
        for f in bool_fields:
            c = f.entry_attr
            rows = conn.execute(
                f"SELECT {c} AS v, COUNT(*) AS c FROM photos WHERE {c} IS NOT NULL GROUP BY {c}"
            ).fetchall()
            bools[f.name] = dict(rows)
        return agg, tags, facets, bools

    agg, tag_rows, facet_rows, bool_rows = await asyncio.to_thread(_agg)
    n = agg["n"]

    stats: dict[str, Any] = {}

    for f in range_fields:
        c = f.entry_attr
        cnt = agg[f"{c}_cnt"]
        if not cnt:
            continue
        st: dict[str, Any] = {
            "type": _RANGE_STAT_TYPE[f.type],
            "min": agg[f"{c}_min"],
            "max": agg[f"{c}_max"],
        }
        if cnt < n:
            st["missing"] = n - cnt
        stats[f.name] = st

    lat_cnt, lon_cnt = agg["lat_cnt"], agg["lon_cnt"]
    if lat_cnt or lon_cnt:
        lat_s: dict[str, Any] = {"min": agg["lat_min"], "max": agg["lat_max"]} if lat_cnt else {}
        lon_s: dict[str, Any] = {"min": agg["lon_min"], "max": agg["lon_max"]} if lon_cnt else {}
        if lat_cnt < n:
            lat_s["missing"] = n - lat_cnt
        if lon_cnt < n:
            lon_s["missing"] = n - lon_cnt
        stats["gps"] = {"type": "gps_bbox", "lat": lat_s, "lon": lon_s}

    if tag_rows:
        stats["tags"] = {"type": "tag_facets", "counts": {tag: cnt for tag, cnt in tag_rows}}

    for f in facet_fields:
        rows = facet_rows.get(f.name) or []
        if rows:
            stats[f.name] = {"type": "string_facets", "counts": {v: c for v, c in rows}}

    for f in bool_fields:
        counts = bool_rows.get(f.name) or {}
        t, fl = int(counts.get(True, 0)), int(counts.get(False, 0))
        if t or fl:
            stats[f.name] = {"type": "bool_counts", "true": t, "false": fl}

    return ManifestSummary(media_count=n, _stats=stats)
