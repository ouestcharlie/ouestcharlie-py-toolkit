"""OuEstCharlie toolkit - shared Python library for photo management agents."""

from .backend import (
    Backend,
    ConfigurationError,
    FileInfo,
    VersionConflictError,
    VersionToken,
    backend_from_config,
)
from .fields import PHOTO_FIELDS, FieldDef, FieldType
from .logging import setup_logging
from .manifest import ManifestStore
from .photo import Photo
from .progress import report_progress
from .schema import (
    LeafManifest,
    ManifestSummary,
    PhotoEntry,
    XmpSidecar,
)
from .server import AgentBase
from .xmp import XmpStore, xmp_lock_dir_for, xmp_path_for

__version__ = "0.1.0"

__all__ = [
    # Core classes
    "AgentBase",
    "Backend",
    "ManifestStore",
    "Photo",
    "XmpStore",
    "report_progress",
    # Data models
    "PhotoEntry",
    "ManifestSummary",
    "LeafManifest",
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
    "xmp_path_for",
    "xmp_lock_dir_for",
]
