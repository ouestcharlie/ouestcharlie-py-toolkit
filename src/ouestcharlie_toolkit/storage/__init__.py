"""Storage: the backend protocol, its implementations and the metadata layout."""

from ouestcharlie_toolkit.storage.backend import (
    Backend,
    ConfigurationError,
    FileInfo,
    PartitionLockToken,
    VersionConflictError,
    VersionToken,
    backend_from_config,
)
from ouestcharlie_toolkit.storage.cloud_mount import CloudMountedBackend
from ouestcharlie_toolkit.storage.hashing import content_hash
from ouestcharlie_toolkit.storage.layout import (
    METADATA_DIR,
    lance_index_path,
    preview_jpeg_path,
    summary_path,
    thumbnail_avif_path,
)
from ouestcharlie_toolkit.storage.local import LocalBackend

__all__ = [
    "METADATA_DIR",
    "Backend",
    "CloudMountedBackend",
    "ConfigurationError",
    "FileInfo",
    "LocalBackend",
    "PartitionLockToken",
    "VersionConflictError",
    "VersionToken",
    "backend_from_config",
    "content_hash",
    "lance_index_path",
    "preview_jpeg_path",
    "summary_path",
    "thumbnail_avif_path",
]
