"""OuEstCharlie toolkit - shared Python library for photo management agents."""

from .backend import Backend, backend_from_config
from .manifest import ManifestStore
from .progress import ProgressReporter
from .schema import (
    ConfigurationError,
    FileInfo,
    LeafManifest,
    ParentManifest,
    PartitionSummary,
    PhotoEntry,
    VersionConflictError,
    VersionToken,
    XmpSidecar,
)
from .server import AgentBase
from .xmp import XmpStore, compute_content_hash, extract_exif, xmp_path_for

__version__ = "0.1.0"

__all__ = [
    # Core classes
    "AgentBase",
    "Backend",
    "ManifestStore",
    "XmpStore",
    "ProgressReporter",
    # Data models
    "PhotoEntry",
    "PartitionSummary",
    "LeafManifest",
    "ParentManifest",
    "XmpSidecar",
    "VersionToken",
    "FileInfo",
    # Exceptions
    "VersionConflictError",
    "ConfigurationError",
    # Utilities
    "backend_from_config",
    "xmp_path_for",
    "extract_exif",
    "compute_content_hash",
]
