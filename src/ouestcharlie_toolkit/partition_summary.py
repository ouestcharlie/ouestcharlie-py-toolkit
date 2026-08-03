"""Partition-level summary statistics computed directly from the Lance index."""

from __future__ import annotations

import asyncio
from typing import Any

import duckdb

from .lance_index import LanceIndex
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


async def compute_partition_summary(lance_index: LanceIndex, partition: str) -> ManifestSummary:
    """Compute ManifestSummary for a single partition.

    Thin wrapper over ``aggregate_where`` scoped to one partition's rows.
    """
    part_esc = partition.replace("'", "''")
    return await aggregate_where(lance_index, f"partition = '{part_esc}'", path=partition)


async def aggregate_where(
    lance_index: LanceIndex, where_clause: str | None, path: str = ""
) -> ManifestSummary:
    """Compute a ManifestSummary-shaped aggregate over any WHERE-filtered subset.

    LanceDB handles the row filter and column projection; DuckDB computes all
    min/max/count aggregates in one SQL pass on the resulting Arrow table. No
    PhotoEntry objects are created. ``where_clause=None`` aggregates the whole
    table. Used both for per-partition summaries (Whitebeard) and for
    runtime, filter-scoped summaries (Wally's ``get_summary`` tool).
    """
    # No .limit(): this must aggregate over the entire filtered set (matching
    # search_where's unlimited facet-count query), not just one page/partition.
    query = lance_index._table.query().select(_AGG_COLUMNS)
    if where_clause:
        query = query.where(where_clause)
    arrow_tbl = await query.to_arrow()

    # DuckDB aggregation is CPU-bound sync — run in a thread pool.
    def _agg() -> tuple[Any, ...] | None:
        conn = duckdb.connect()
        conn.register("photos", arrow_tbl)
        return conn.execute(_AGG_SQL).fetchone()

    row = await asyncio.to_thread(_agg)
    if not row:
        return ManifestSummary(path=path)

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

    return ManifestSummary(path=path, photo_count=n, _stats=stats)
