"""Compute summary statistics directly from the Lance index."""

from __future__ import annotations

import asyncio
from typing import Any

import duckdb

from .lance_index import FtsFilter, LanceIndex
from .schema import ManifestSummary

_AGG_COLUMNS = ["date_taken", "rating", "width", "height", "gps_lat", "gps_lon"]

_AGG_SQL = """
SELECT
    COUNT(*)         AS n,
    MIN(date_taken)  AS date_min,  MAX(date_taken) AS date_max,  COUNT(date_taken)  AS date_cnt,
    MIN(rating)      AS rating_min, MAX(rating)    AS rating_max, COUNT(rating)      AS rating_cnt,
    MIN(width)       AS width_min,  MAX(width)     AS width_max,  COUNT(width)       AS width_cnt,
    MIN(height)      AS height_min, MAX(height)    AS height_max, COUNT(height)      AS height_cnt,
    MIN(gps_lat)     AS lat_min,    MAX(gps_lat)   AS lat_max,    COUNT(gps_lat)     AS lat_cnt,
    MIN(gps_lon)     AS lon_min,    MAX(gps_lon)   AS lon_max,    COUNT(gps_lon)     AS lon_cnt
FROM photos
"""

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
    DuckDB computes all min/max/count aggregates, plus tag facet counts.
    ``where_clause=None`` with no ``fts_filter`` aggregates the whole table.
    """
    # No .limit(): this must aggregate over the entire filtered set (matching
    # search_where's unlimited facet-count query), not just one page/partition.
    query = lance_index._table.query()
    if where_clause:
        query = query.where(where_clause)
    if fts_filter:
        query = query.nearest_to_text(fts_filter.query, columns=fts_filter.columns)
    arrow_tbl = await query.select([*_AGG_COLUMNS, "tags"]).to_arrow()

    # DuckDB aggregation is CPU-bound sync — run in a thread pool.
    def _agg() -> tuple[tuple[Any, ...] | None, list[tuple[str, int]]]:
        conn = duckdb.connect()
        conn.register("photos", arrow_tbl)
        agg_row = conn.execute(_AGG_SQL).fetchone()
        tag_rows = conn.execute(_TAG_FACETS_SQL).fetchall()
        return agg_row, tag_rows

    row, tag_rows = await asyncio.to_thread(_agg)
    if not row:
        return ManifestSummary()

    (
        n,
        date_min,
        date_max,
        date_cnt,
        rating_min,
        rating_max,
        rating_cnt,
        width_min,
        width_max,
        width_cnt,
        height_min,
        height_max,
        height_cnt,
        lat_min,
        lat_max,
        lat_cnt,
        lon_min,
        lon_max,
        lon_cnt,
    ) = row

    stats: dict[str, Any] = {}

    if date_cnt:
        stat: dict[str, Any] = {"type": "date_range", "min": date_min, "max": date_max}
        if date_cnt < n:
            stat["missing"] = n - date_cnt
        stats["dateTaken"] = stat

    for min_v, max_v, cnt, name in [
        (rating_min, rating_max, rating_cnt, "rating"),
        (width_min, width_max, width_cnt, "width"),
        (height_min, height_max, height_cnt, "height"),
    ]:
        if cnt:
            st: dict[str, Any] = {"type": "int_range", "min": min_v, "max": max_v}
            if cnt < n:
                st["missing"] = n - cnt
            stats[name] = st

    if lat_cnt or lon_cnt:
        lat_s: dict[str, Any] = {"min": lat_min, "max": lat_max} if lat_cnt else {}
        lon_s: dict[str, Any] = {"min": lon_min, "max": lon_max} if lon_cnt else {}
        if lat_cnt < n:
            lat_s["missing"] = n - lat_cnt
        if lon_cnt < n:
            lon_s["missing"] = n - lon_cnt
        stats["gps"] = {"type": "gps_bbox", "lat": lat_s, "lon": lon_s}

    if tag_rows:
        stats["tags"] = {"type": "tag_facets", "counts": {tag: cnt for tag, cnt in tag_rows}}

    return ManifestSummary(photo_count=n, _stats=stats)
