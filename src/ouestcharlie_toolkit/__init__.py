"""OuEstCharlie toolkit - shared Python library for photo management agents.

Sub-packages, lowest layer first (a module imports only from its own layer or below):

- ``storage``     backend protocol and implementations, ``.ouestcharlie/`` layout
- ``model``       searchable fields, tags, entries, summaries, schema version
- ``media``       Photo and Video: identity and metadata extraction
- ``xmp``         sidecar naming, parsing, serialization, XmpStore
- ``index``       LanceDB index, summaries, summary.json store
- ``renditions``  thumbnails and previews (the only user of ouestcharlie_imageproc)
- ``agent``       MCP server base class and logging

Agents import from a sub-package (``from ouestcharlie_toolkit.index import LanceIndex``),
never from a leaf module or a ``_private`` name. The names below are shortcuts for the
most used ones.
"""

from importlib.metadata import version

from ouestcharlie_toolkit.agent import AgentBase, setup_logging
from ouestcharlie_toolkit.index import SummaryStore
from ouestcharlie_toolkit.media import (
    PHOTO_SUFFIXES,
    VIDEO_SUFFIXES,
    Photo,
    Video,
    video_identity_hash,
)
from ouestcharlie_toolkit.model import (
    PHOTO_FIELDS,
    FieldDef,
    FieldType,
    ManifestSummary,
    PhotoEntry,
    XmpSidecar,
)
from ouestcharlie_toolkit.storage import (
    Backend,
    ConfigurationError,
    FileInfo,
    VersionConflictError,
    VersionToken,
    backend_from_config,
)
from ouestcharlie_toolkit.xmp import XmpStore, sidecar_candidates, xmp_path_for

__version__ = version("ouestcharlie-py-toolkit")

__all__ = [
    # Core classes
    "AgentBase",
    "Backend",
    "SummaryStore",
    "Photo",
    "Video",
    "XmpStore",
    "PHOTO_SUFFIXES",
    "VIDEO_SUFFIXES",
    "video_identity_hash",
    # Data models
    "PhotoEntry",
    "ManifestSummary",
    "XmpSidecar",
    "VersionToken",
    "FileInfo",
    # Exceptions
    "VersionConflictError",
    "ConfigurationError",
    # Field configuration
    "FieldType",
    "FieldDef",
    "PHOTO_FIELDS",
    # Utilities
    "backend_from_config",
    "setup_logging",
    "sidecar_candidates",
    "xmp_path_for",
]
