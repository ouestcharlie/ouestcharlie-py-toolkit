"""OuEstCharlie toolkit - shared Python library for photo management agents."""

from importlib.metadata import version

from ouestcharlie_toolkit.logging import setup_logging
from ouestcharlie_toolkit.manifest import ManifestStore
from ouestcharlie_toolkit.media.photo import Photo
from ouestcharlie_toolkit.media.suffixes import PHOTO_SUFFIXES, VIDEO_SUFFIXES
from ouestcharlie_toolkit.media.video import Video, video_identity_hash
from ouestcharlie_toolkit.model.entries import PhotoEntry, XmpSidecar
from ouestcharlie_toolkit.model.fields import PHOTO_FIELDS, FieldDef, FieldType
from ouestcharlie_toolkit.model.summary import ManifestSummary
from ouestcharlie_toolkit.server import AgentBase
from ouestcharlie_toolkit.storage.backend import (
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
    "ManifestStore",
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
