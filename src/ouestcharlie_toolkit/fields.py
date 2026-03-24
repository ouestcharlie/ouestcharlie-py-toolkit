"""Field type configuration for searchable photo metadata.

This module defines the searchable field taxonomy and the default configuration
that drives matching, pruning, and query building in Wally (and future agents),
as well as XmpSidecar → PhotoEntry mapping in Whitebeard.

Adding a new searchable field requires only adding a FieldDef entry to PHOTO_FIELDS —
no changes needed in matching, pruning, indexing, or summary serialisation logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class FieldType(Enum):
    """Taxonomy of searchable field types, each with distinct match and pruning semantics."""

    DATE_RANGE        = auto()  # datetime min/max bounds; pruneable
    INT_RANGE         = auto()  # int min/max bounds; pruneable
    STRING_COLLECTION = auto()  # list[str] with AND exact match (e.g. tags); bloom-filter pruning
    STRING_MATCH      = auto()  # str with case-insensitive substring match; no summary pruning
    GPS_BOX           = auto()  # (lat, lon) point; partition summary bbox
    DESCRIPTIVE       = auto()  # placeholder: future similarity/embedding match


@dataclass(frozen=True)
class FieldDef:
    """Definition of a single searchable field.

    Attributes:
        name:               Logical field name; used as the JSON key in partition summaries
                            and as the key in SearchPredicate.filters.
        type:               Match and pruning semantics for this field.
        entry_attr:         Attribute name on PhotoEntry that holds this field's value.
        summary_range:      True if the field contributes min/max range stats to the
                            partition summary, stored as ``{name}_min`` / ``{name}_max``.
                            Applies to DATE_RANGE and INT_RANGE fields.
        summary_bloom_attr: Attribute name on PartitionSummary for the bloom filter.
                            Set for STRING_COLLECTION fields that support bloom pruning.
        summary_gps_bbox:   True if the field contributes a GPS bounding box to the partition
                            summary (minLat/maxLat/minLon/maxLon). Applies to GPS_BOX fields.
        sidecar_attr:       Attribute name on XmpSidecar to read when building a PhotoEntry.
                            None means the field has no direct XmpSidecar source (e.g. it is
                            derived or supplied externally, like filename or content_hash).
    """

    name: str
    type: FieldType
    entry_attr: str
    summary_range: bool = False
    summary_bloom_attr: str | None = None
    summary_gps_bbox: bool = False
    sidecar_attr: str | None = None


# Searchable field configuration for OuEstCharlie photos.
#
# Fields with summary_range=True support partition-level range pruning (min/max).
# Fields with summary_bloom_attr support partition-level bloom-filter pruning.
# Fields with summary_gps_bbox=True support partition-level GPS bbox pruning (minLat/maxLat/minLon/maxLon).
# Fields without any summary attribute are searchable at leaf-scan level only.
# Fields without sidecar_attr are populated by the caller (e.g. filename, content_hash).
PHOTO_FIELDS: list[FieldDef] = [
    # Date/time range — partition summary pruning via min/max
    FieldDef(name="dateTaken", type=FieldType.DATE_RANGE, entry_attr="date_taken", summary_range=True, sidecar_attr="date_taken"),

    # Integer ranges — partition summary pruning via min/max
    FieldDef(name="rating",      type=FieldType.INT_RANGE, entry_attr="rating",      summary_range=True,  sidecar_attr="rating"),
    FieldDef(name="width",       type=FieldType.INT_RANGE, entry_attr="width",       summary_range=True,  sidecar_attr="width"),
    FieldDef(name="height",      type=FieldType.INT_RANGE, entry_attr="height",      summary_range=True,  sidecar_attr="height"),
    FieldDef(name="orientation", type=FieldType.INT_RANGE, entry_attr="orientation",                      sidecar_attr="orientation"),

    # String collection — bloom-filter pruning
    FieldDef(name="tags", type=FieldType.STRING_COLLECTION, entry_attr="tags", summary_bloom_attr="tags_bloom", sidecar_attr="tags"),

    # String match — case-insensitive substring; no summary pruning
    # Note: XmpSidecar uses camera_make/camera_model; PhotoEntry uses make/model
    FieldDef(name="make",  type=FieldType.STRING_MATCH, entry_attr="make",  sidecar_attr="camera_make"),
    FieldDef(name="model", type=FieldType.STRING_MATCH, entry_attr="model", sidecar_attr="camera_model"),

    # GPS bounding box — partition summary bbox + Wally bbox filter/pruning
    FieldDef(name="gps", type=FieldType.GPS_BOX, entry_attr="gps", summary_gps_bbox=True, sidecar_attr="gps"),
]
