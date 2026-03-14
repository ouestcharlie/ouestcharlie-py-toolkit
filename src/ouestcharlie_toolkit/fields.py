"""Field type configuration for searchable photo metadata.

This module defines the searchable field taxonomy and the default configuration
that drives matching, pruning, and query building in Wally (and future agents).

Adding a new searchable field requires only adding a FieldDef entry to PHOTO_FIELDS —
no changes needed in matching or pruning logic.
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
    DESCRIPTIVE       = auto()  # placeholder: future similarity/embedding match


@dataclass(frozen=True)
class FieldDef:
    """Definition of a single searchable field.

    Attributes:
        name:              Logical field name used as the key in SearchPredicate.filters.
        type:              Match and pruning semantics for this field.
        entry_attr:        Attribute name on PhotoEntry that holds this field's value.
        summary_min_attr:  Attribute name on PartitionSummary for the range lower bound.
                           Only set for DATE_RANGE and INT_RANGE fields.
        summary_max_attr:  Attribute name on PartitionSummary for the range upper bound.
                           Only set for DATE_RANGE and INT_RANGE fields.
    """

    name: str
    type: FieldType
    entry_attr: str
    summary_min_attr: str | None = None
    summary_max_attr: str | None = None


# Default searchable field configuration for OuEstCharlie photos.
# The attribute names reference existing fields on PhotoEntry and PartitionSummary
# in schema.py — those dataclasses are not modified.
PHOTO_FIELDS: list[FieldDef] = [
    FieldDef(
        name="date",
        type=FieldType.DATE_RANGE,
        entry_attr="date_taken",
        summary_min_attr="date_min",
        summary_max_attr="date_max",
    ),
    FieldDef(
        name="rating",
        type=FieldType.INT_RANGE,
        entry_attr="rating",
        summary_min_attr="rating_min",
        summary_max_attr="rating_max",
    ),
    FieldDef(
        name="tags",
        type=FieldType.STRING_COLLECTION,
        entry_attr="tags",
    ),
    FieldDef(
        name="make",
        type=FieldType.STRING_MATCH,
        entry_attr="make",
    ),
    FieldDef(
        name="model",
        type=FieldType.STRING_MATCH,
        entry_attr="model",
    ),
]
