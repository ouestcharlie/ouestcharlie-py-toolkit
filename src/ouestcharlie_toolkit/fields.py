"""Field type configuration for searchable photo metadata.

This module defines the searchable field taxonomy and the default configuration
that drives matching, pruning, and query building in Wally (and future agents),
as well as XmpSidecar → PhotoEntry mapping in Whitebeard.

Adding a new searchable field requires only adding a FieldDef entry to PHOTO_FIELDS —
no changes needed in matching, pruning, or indexing logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class FieldType(Enum):
    """Taxonomy of searchable field types, each with distinct match and pruning semantics."""

    DATE_RANGE        = auto()  # datetime min/max bounds; pruneable via PartitionSummary range stats
    INT_RANGE         = auto()  # int min/max bounds; pruneable via PartitionSummary range stats
    STRING_COLLECTION = auto()  # list[str] with AND exact match (e.g. tags); no summary pruning
    STRING_MATCH      = auto()  # str with case-insensitive substring match; no summary pruning
    GPS_BOX           = auto()  # (lat, lon) bounding box; matching not yet implemented in Wally
    DESCRIPTIVE       = auto()  # placeholder: future similarity/embedding match


@dataclass(frozen=True)
class FieldDef:
    """Definition of a single searchable field.

    Attributes:
        name:              Logical field name used as the key in SearchPredicate.filters.
        type:              Match and pruning semantics for this field.
        entry_attr:        Attribute name on PhotoEntry that holds this field's value.
        summary_min_attr:  Attribute name on PartitionSummary for the range lower bound.
                           Only set for DATE_RANGE and INT_RANGE fields with summary pruning.
        summary_max_attr:  Attribute name on PartitionSummary for the range upper bound.
                           Only set for DATE_RANGE and INT_RANGE fields with summary pruning.
        sidecar_attr:      Attribute name on XmpSidecar to read when building a PhotoEntry.
                           None means the field has no direct XmpSidecar source (e.g. it is
                           derived or supplied externally, like filename or content_hash).
    """

    name: str
    type: FieldType
    entry_attr: str
    summary_min_attr: str | None = None
    summary_max_attr: str | None = None
    sidecar_attr: str | None = None


# Default searchable field configuration for OuEstCharlie photos.
#
# The attribute names reference existing fields on PhotoEntry, PartitionSummary,
# and XmpSidecar in schema.py — those dataclasses are not modified.
#
# Fields without summary_min/max_attr are searchable at leaf-scan level only
# (no parent-level pruning). Fields without sidecar_attr are populated by
# the caller of _sidecar_to_entry (e.g. filename, content_hash).
PHOTO_FIELDS: list[FieldDef] = [
    # Range fields — summary pruning supported via existing PartitionSummary attrs
    FieldDef(
        name="date",
        type=FieldType.DATE_RANGE,
        entry_attr="date_taken",
        summary_min_attr="date_min",
        summary_max_attr="date_max",
        sidecar_attr="date_taken",
    ),
    FieldDef(
        name="rating",
        type=FieldType.INT_RANGE,
        entry_attr="rating",
        summary_min_attr="rating_min",
        summary_max_attr="rating_max",
        sidecar_attr="rating",
    ),

    # Int fields — leaf-scannable only; no corresponding PartitionSummary attrs
    FieldDef(name="width",       type=FieldType.INT_RANGE, entry_attr="width",       sidecar_attr="width"),
    FieldDef(name="height",      type=FieldType.INT_RANGE, entry_attr="height",      sidecar_attr="height"),
    FieldDef(name="orientation", type=FieldType.INT_RANGE, entry_attr="orientation", sidecar_attr="orientation"),

    # String collection — AND exact match
    FieldDef(name="tags",  type=FieldType.STRING_COLLECTION, entry_attr="tags",  sidecar_attr="tags"),

    # String match — case-insensitive substring
    # Note: XmpSidecar uses camera_make/camera_model; PhotoEntry uses make/model
    FieldDef(name="make",  type=FieldType.STRING_MATCH, entry_attr="make",  sidecar_attr="camera_make"),
    FieldDef(name="model", type=FieldType.STRING_MATCH, entry_attr="model", sidecar_attr="camera_model"),

    # GPS bounding box — indexed but Wally matching not yet implemented (gracefully skipped)
    FieldDef(name="gps",   type=FieldType.GPS_BOX,      entry_attr="gps",   sidecar_attr="gps"),
]
