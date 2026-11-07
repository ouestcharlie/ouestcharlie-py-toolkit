"""Library summaries: aggregate statistics and the root summary.json marker."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ouestcharlie_toolkit.model.fields import PHOTO_FIELDS, FieldType
from ouestcharlie_toolkit.model.version import SCHEMA_VERSION


class ManifestSummary:
    """Aggregate statistics over a set of media items (count plus per-field ranges).

    ``media_count`` (JSON ``mediaCount``) counts all matching items — photos and
    videos alike.

    Per-field statistics are stored in ``_stats`` as typed dicts that mirror
    the JSON serialisation format:

    - date range:  ``{"type": "date_range", "min": datetime, "max": datetime}``
    - int range:   ``{"type": "int_range",  "min": int,      "max": int}``

    Field stats are accessed via normal attribute syntax (``__getattr__``),
    e.g. ``summary.date["min"]``, ``summary.rating["max"]``.

    Adding a new summarisable field requires only a ``FieldDef`` entry in
    ``fields.py`` — no changes needed here.
    """

    def __init__(
        self,
        media_count: int = 0,
        _stats: dict[str, dict[str, Any]] | None = None,
        _extra: dict[str, Any] | None = None,
    ) -> None:
        self.media_count = media_count
        self._stats: dict[str, dict[str, Any]] = dict(_stats) if _stats else {}
        self._extra: dict[str, Any] = dict(_extra) if _extra is not None else {}

    def __getattr__(self, name: str) -> Any:
        """Return the typed stat dict for a field,
        e.g. summary.rating → {"type": "int_range", ...}."""
        return self.__dict__.get("_stats", {}).get(name)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ManifestSummary):
            return NotImplemented
        return self.media_count == other.media_count and self._stats == other._stats

    def __repr__(self) -> str:
        parts = [f"media_count={self.media_count}"]
        for k, v in self._stats.items():
            parts.append(f"{k}={v!r}")
        return f"ManifestSummary({', '.join(parts)})"


@dataclass
class RootSummary:
    """Thin schema-version marker for a backend.

    Written at <backend-root>/.ouestcharlie/summary.json, once per full
    indexing session. Its only purpose is letting readers (Wally) detect an
    unindexed library or a stale schema version without opening the LanceDB
    index. Per-partition and library-wide statistics are computed at query
    time instead (see ``compute_summary`` in ``partition_summary.py``).
    """

    schema_version: int
    last_indexed_at: datetime | None = None
    _extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

# These convert between dataclass instances and JSON-compatible dicts.
# Unknown fields are preserved via the _extra attribute.


def summary_to_dict(s: ManifestSummary) -> dict[str, Any]:
    d: dict[str, Any] = {
        "mediaCount": s.media_count,
    }
    # _stats already mirrors the JSON structure; only datetime and bytes need conversion.
    for name, stat in s._stats.items():
        t = stat.get("type")
        if t == "date_range":
            out: dict[str, Any] = {"type": "date_range"}
            if stat.get("min") is not None:
                out["min"] = stat["min"].isoformat()
            if stat.get("max") is not None:
                out["max"] = stat["max"].isoformat()
            if stat.get("missing"):
                out["missing"] = stat["missing"]
            d[name] = out
        elif t in (
            "int_range",
            "float_range",
            "tag_facets",
            "string_facets",
            "bool_counts",
            "gps_bbox",
        ):
            # Already JSON-native (numbers, strings, count dicts, and lat/lon boxes).
            d[name] = stat
    d.update(s._extra)
    return d


def summary_from_dict(d: dict[str, Any]) -> ManifestSummary:
    known_keys = {"mediaCount", "hashes"}
    stats: dict[str, dict[str, Any]] = {}
    for fd in PHOTO_FIELDS:
        known_keys.add(fd.name)
        stat = d.get(fd.name)
        if not isinstance(stat, dict):
            continue
        if fd.summary_range and fd.type is FieldType.DATE_RANGE:
            parsed: dict[str, Any] = {
                "type": "date_range",
                "min": datetime.fromisoformat(stat["min"]) if "min" in stat else None,
                "max": datetime.fromisoformat(stat["max"]) if "max" in stat else None,
            }
            if stat.get("missing"):
                parsed["missing"] = stat["missing"]
            stats[fd.name] = parsed
        elif fd.summary_range and fd.type is FieldType.INT_RANGE:
            parsed = {
                "type": "int_range",
                "min": stat.get("min"),
                "max": stat.get("max"),
            }
            if stat.get("missing"):
                parsed["missing"] = stat["missing"]
            stats[fd.name] = parsed
        elif fd.summary_range and fd.type is FieldType.FLOAT_RANGE:
            parsed = {
                "type": "float_range",
                "min": stat.get("min"),
                "max": stat.get("max"),
            }
            if stat.get("missing"):
                parsed["missing"] = stat["missing"]
            stats[fd.name] = parsed
        elif fd.summary_facet and stat.get("type") == "string_facets":
            counts = stat.get("counts")
            if isinstance(counts, dict):
                stats[fd.name] = {"type": "string_facets", "counts": dict(counts)}
        elif fd.type is FieldType.BOOL and stat.get("type") == "bool_counts":
            stats[fd.name] = {
                "type": "bool_counts",
                "true": stat.get("true", 0),
                "false": stat.get("false", 0),
            }
        elif fd.type is FieldType.GPS_BOX and stat.get("type") == "gps_bbox":
            # JSON-native lat/lon boxes ({"min", "max", "missing"}); copy as-is.
            parsed = {"type": "gps_bbox"}
            if isinstance(stat.get("lat"), dict):
                parsed["lat"] = dict(stat["lat"])
            if isinstance(stat.get("lon"), dict):
                parsed["lon"] = dict(stat["lon"])
            stats[fd.name] = parsed
    hashes_stat = d.get("hashes")
    if isinstance(hashes_stat, dict) and hashes_stat.get("value"):
        stats["hashes"] = {
            "type": "bloom",
            "value": bytes.fromhex(hashes_stat["value"]),
        }
    extra = {k: v for k, v in d.items() if k not in known_keys}
    return ManifestSummary(
        media_count=d.get("mediaCount", 0),
        _stats=stats,
        _extra=extra,
    )


def serialize_summary(s: RootSummary) -> dict[str, Any]:
    """Serialize a RootSummary to a JSON-compatible dict."""
    d: dict[str, Any] = {"schemaVersion": s.schema_version}
    if s.last_indexed_at is not None:
        d["lastIndexedAt"] = s.last_indexed_at.isoformat()
    d.update(s._extra)
    return d


def deserialize_summary(d: dict[str, Any]) -> RootSummary:
    """Deserialize a JSON dict into a RootSummary, preserving unknown fields.

    Tolerant of the legacy bulky shape (a ``partitions`` list) written before
    this thin-marker redesign — that field is simply ignored, not preserved.
    """
    known_keys = {"schemaVersion", "lastIndexedAt", "partitions"}
    extra = {k: v for k, v in d.items() if k not in known_keys}
    last_indexed_raw = d.get("lastIndexedAt")
    return RootSummary(
        schema_version=d.get("schemaVersion", SCHEMA_VERSION),
        last_indexed_at=datetime.fromisoformat(last_indexed_raw) if last_indexed_raw else None,
        _extra=extra,
    )
