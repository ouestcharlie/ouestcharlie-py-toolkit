"""Data model: searchable fields, tags, per-photo entries, summaries and schema version."""

from ouestcharlie_toolkit.model.entries import PhotoEntry, XmpSidecar
from ouestcharlie_toolkit.model.fields import PHOTO_FIELDS, FieldDef, FieldType, is_sortable
from ouestcharlie_toolkit.model.summary import (
    ManifestSummary,
    RootSummary,
    deserialize_summary,
    serialize_summary,
    summary_from_dict,
    summary_to_dict,
)
from ouestcharlie_toolkit.model.tags import (
    DEFAULT_EXCLUDED_TAG_PREFIXES,
    TAG_SEPARATOR,
    fold,
    normalize_path,
    normalize_paths,
)
from ouestcharlie_toolkit.model.thumbnails import ThumbnailChunk, ThumbnailGridLayout
from ouestcharlie_toolkit.model.version import (
    LOWEST_SCHEMA_VERSION,
    OUESTCHARLIE_NS,
    SCHEMA_VERSION,
    is_index_schema_compatible,
)

__all__ = [
    "DEFAULT_EXCLUDED_TAG_PREFIXES",
    "LOWEST_SCHEMA_VERSION",
    "OUESTCHARLIE_NS",
    "PHOTO_FIELDS",
    "SCHEMA_VERSION",
    "TAG_SEPARATOR",
    "FieldDef",
    "FieldType",
    "ManifestSummary",
    "PhotoEntry",
    "RootSummary",
    "ThumbnailChunk",
    "ThumbnailGridLayout",
    "XmpSidecar",
    "deserialize_summary",
    "fold",
    "is_index_schema_compatible",
    "is_sortable",
    "normalize_path",
    "normalize_paths",
    "serialize_summary",
    "summary_from_dict",
    "summary_to_dict",
]
