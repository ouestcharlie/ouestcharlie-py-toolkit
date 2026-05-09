"""Backend abstraction for storage operations."""

from __future__ import annotations

import logging
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Version token and file info
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VersionToken:
    """Opaque version token returned by backends. Callers pass it back to
    write_conditional without inspecting its value."""

    value: Any


@dataclass(frozen=True)
class FileInfo:
    """Metadata about a file returned by Backend.list_files."""

    path: str
    version: VersionToken


@dataclass(frozen=True)
class PartitionLockToken:
    """Opaque proof that the caller holds the exclusive partition lock.

    Pass to write_conditional, XmpStore.write, and ManifestStore.write_leaf
    to skip per-file cross-process lock acquisition for files in this partition.
    """

    partition: str


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class VersionConflictError(Exception):
    """Raised when a conditional write fails because the file was modified."""

    def __init__(self, path: str, expected: VersionToken, actual: VersionToken) -> None:
        self.path = path
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"Version conflict on {path}: expected {expected.value}, got {actual.value}"
        )


class ConfigurationError(Exception):
    """Raised for invalid or missing configuration (backend root missing, bad credentials, etc.)."""


# ---------------------------------------------------------------------------
# Backend protocol
# ---------------------------------------------------------------------------


class Backend(Protocol):
    """Protocol defining the storage interface all backends must implement.

    All paths are relative to the backend root.
    """

    async def read(self, path: str) -> tuple[bytes, VersionToken]:
        """Read file contents and return data with its version token.

        Raises:
            FileNotFoundError: If the file does not exist.
        """
        ...

    async def write_conditional(
        self,
        path: str,
        data: bytes,
        expected_version: VersionToken,
    ) -> VersionToken:
        """Write file if its version matches expected_version (optimistic concurrency).

        Does not acquire any cross-process lock — callers are responsible for
        holding ``Backend.partition_lock()`` before calling this method.

        Args:
            path: Backend-relative path to the file.
            data: New file contents.
            expected_version: Version token from the last read.

        Returns:
            New version token after successful write.

        Raises:
            VersionConflictError: If the file's version doesn't match expected_version.
            FileNotFoundError: If the file does not exist.
        """
        ...

    def partition_lock(self, partition: str) -> AbstractAsyncContextManager[PartitionLockToken]:
        """Acquire an exclusive cross-process lock for a partition.

        Lock file: .ouestcharlie/{partition}/partition.lock
        Root lock (for summary.json): pass partition="" → .ouestcharlie/partition.lock

        Callers must hold this lock before calling write_conditional,
        XmpStore.write(), or ManifestStore.write_leaf().  Store methods
        that accept a PartitionLockToken skip acquiring the lock themselves;
        without a token they acquire it internally.
        """
        ...

    async def write_new(self, path: str, data: bytes) -> VersionToken:
        """Write a new file. Fails if the file already exists.

        Returns:
            Version token of the newly created file.

        Raises:
            FileExistsError: If the file already exists.
        """
        ...

    async def list_dirs(self, prefix: str) -> list[str]:
        """List immediate subdirectory paths under prefix.

        Args:
            prefix: Directory path relative to the backend root.

        Returns:
            List of subdirectory paths relative to the backend root.
            Returns an empty list if prefix does not exist.
        """
        ...

    async def list_files(
        self,
        prefix: str,
        suffixes: frozenset[str] | None = None,
    ) -> list[FileInfo]:
        """List direct-child files under prefix, optionally filtered by extension.

        Args:
            prefix: Directory path relative to the backend root.
            suffixes: Optional set of lowercase extensions to include
                (e.g. ``frozenset({".jpg", ".heic"})``).  When ``None``,
                all direct-child files are returned.

        Returns:
            List of FileInfo objects with paths and version tokens.
        """
        ...

    async def exists(self, path: str) -> bool:
        """Check if a file exists at the given path."""
        ...

    async def delete(self, path: str) -> None:
        """Delete a file.

        Raises:
            FileNotFoundError: If the file does not exist.
        """
        ...

    async def delete_dir(self, path: str) -> None:
        """Delete a directory and all its contents recursively.

        Args:
            path: Backend-relative path to the directory to remove.

        Raises:
            FileNotFoundError: If the directory does not exist.
            ValueError: If path refers to a file, not a directory.
        """
        ...

    async def local_path(self, path: str) -> Path:
        """Return a local filesystem path for this backend-relative path.

        For local and cloud-mounted (FUSE) backends this is the resolved path to
        the file on disk. Backends that need to fetch the file remotely may
        download it to a temporary location and return that path instead.

        """
        ...

    async def content_hash(self, path: str) -> str:
        """Return the canonical content hash for this file.

        Canonical format: BLAKE3 truncated to 128 bits, base64url-encoded without
        padding — a 22-character URL- and filename-safe string.

        Default implementation reads the file and computes the BLAKE3 hash.
        Remote backends can override to fetch the
        provider checksum from their REST API without downloading the file.

        Raises:
            ValueError: If the file is empty (zero bytes).
            FileNotFoundError: If the file does not exist.
        """
        ...


def backend_from_config(config: dict[str, str]) -> Backend:
    """Factory function to create a Backend instance from configuration.

    Args:
        config: Backend configuration dict with 'type' and type-specific fields.
                Example: {"type": "filesystem", "root": "/path/to/photos"}

    Returns:
        Backend instance matching the configured type.

    Raises:
        ConfigurationError: If config is invalid or backend type is unsupported.
    """
    name = config.get("name")
    backend_type = config.get("type")

    if backend_type == "filesystem":
        from .backends.local import LocalBackend

        root = config.get("root")
        if not root:
            raise ConfigurationError("filesystem backend requires 'root' field")
        _log.debug(f"Backend '{name}', initialized as 'filesystem' with root path '{root}'")
        return LocalBackend(root)

    if backend_type == "cloud_mount":
        from .backends.cloud_mount import CloudMountedBackend

        root = config.get("root")
        if not root:
            raise ConfigurationError("cloud_mount backend requires 'root' field")
        _log.debug(f"Backend '{name}', initialized as 'cloud_mount' with root path '{root}'")
        return CloudMountedBackend(root)

    # Future backends: s3, gcs, adls2, onedrive, kdrive
    raise ConfigurationError(f"Unsupported backend type: {backend_type}")
