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
    """Taxonomy of searchable field types, each with distinct match semantics."""

    DATE_RANGE = auto()  # datetime min/max bounds; partition summary pruning via min/max
    INT_RANGE = auto()  # int min/max bounds; partition summary pruning via min/max
    FLOAT_RANGE = auto()  # float min/max bounds; no summary pruning
    STRING_COLLECTION = auto()  # list[str] with AND exact match (e.g. tags)
    STRING_MATCH = auto()  # str with case-insensitive substring match
    TEXT = auto()  # str; full-text search via FTS index, returns relevance score
    GPS_BOX = auto()  # (lat, lon) point
    DESCRIPTIVE = auto()  # placeholder: future similarity/embedding match


@dataclass(frozen=True)
class FieldDef:
    """Definition of a single searchable field.

    Attributes:
        name:               Logical field name; used as the JSON key in partition summaries
                            and as the key in SearchPredicate.filters.
        type:               Match and pruning semantics for this field.
        entry_attr:         Attribute name on PhotoEntry that holds this field's value.
        summary_range:  True if the field contributes min/max range stats to the
                        partition summary. Applies to DATE_RANGE and INT_RANGE fields.
        sidecar_attr:   Attribute name on XmpSidecar to read when building a PhotoEntry.
                        None means the field has no direct XmpSidecar source (e.g. it is
                        derived or supplied externally, like filename or content_hash).
        label:          Human-readable label for UI / list_search_fields.
    """

    name: str
    type: FieldType
    entry_attr: str
    summary_range: bool = False
    sidecar_attr: str | None = None
    label: str | None = None


# Searchable field configuration for OuEstCharlie photos.
#
# Fields with summary_range=True contribute min/max stats to the partition summary
# and support partition-level range pruning (DATE_RANGE and INT_RANGE only).
# Fields without sidecar_attr are populated by the caller (e.g. filename, content_hash).
PHOTO_FIELDS: list[FieldDef] = [
    # Date/time range — partition summary pruning via min/max
    FieldDef(
        name="dateTaken",
        type=FieldType.DATE_RANGE,
        entry_attr="date_taken",
        summary_range=True,
        sidecar_attr="date_taken",
    ),
    # Integer ranges — partition summary pruning via min/max
    FieldDef(
        name="rating",
        type=FieldType.INT_RANGE,
        entry_attr="rating",
        summary_range=True,
        sidecar_attr="rating",
    ),
    FieldDef(
        name="width",
        type=FieldType.INT_RANGE,
        entry_attr="width",
        summary_range=True,
        sidecar_attr="width",
    ),
    FieldDef(
        name="height",
        type=FieldType.INT_RANGE,
        entry_attr="height",
        summary_range=True,
        sidecar_attr="height",
    ),
    FieldDef(
        name="orientation",
        type=FieldType.INT_RANGE,
        entry_attr="orientation",
        sidecar_attr="orientation",
    ),
    # String collection — AND exact match on list elements
    FieldDef(
        name="tags",
        type=FieldType.STRING_COLLECTION,
        entry_attr="tags",
        sidecar_attr="tags",
    ),
    # String match — case-insensitive substring
    # Note: XmpSidecar uses camera_make/camera_model; PhotoEntry uses make/model
    FieldDef(
        name="make",
        type=FieldType.STRING_MATCH,
        entry_attr="make",
        sidecar_attr="camera_make",
    ),
    FieldDef(
        name="model",
        type=FieldType.STRING_MATCH,
        entry_attr="model",
        sidecar_attr="camera_model",
    ),
    # GPS bounding box
    FieldDef(
        name="gps",
        type=FieldType.GPS_BOX,
        entry_attr="gps",
        sidecar_attr="gps",
    ),
    # Full-text search on dc:description caption
    FieldDef(
        name="description",
        type=FieldType.TEXT,
        entry_attr="description",
        sidecar_attr="description",
        label="Description",
    ),
    # Camera shoot settings — float/int range filters
    FieldDef(
        name="isoSpeed",
        type=FieldType.INT_RANGE,
        entry_attr="iso_speed",
        sidecar_attr="iso_speed",
        label="ISO speed",
    ),
    FieldDef(
        name="aperture",
        type=FieldType.FLOAT_RANGE,
        entry_attr="aperture",
        sidecar_attr="aperture",
        label="Aperture (f-number)",
    ),
    FieldDef(
        name="exposureTime",
        type=FieldType.FLOAT_RANGE,
        entry_attr="exposure_time",
        sidecar_attr="exposure_time",
        label="Exposure time (s)",
    ),
    FieldDef(
        name="focalLength",
        type=FieldType.FLOAT_RANGE,
        entry_attr="focal_length",
        sidecar_attr="focal_length",
        label="Focal length (mm)",
    ),
    FieldDef(
        name="focalLength35mm",
        type=FieldType.INT_RANGE,
        entry_attr="focal_length_35mm",
        sidecar_attr="focal_length_35mm",
        label="Focal length 35mm equiv.",
    ),
    FieldDef(
        name="lensModel",
        type=FieldType.STRING_MATCH,
        entry_attr="lens_model",
        sidecar_attr="lens_model",
        label="Lens",
    ),
]
