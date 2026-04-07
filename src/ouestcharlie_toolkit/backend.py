"""Backend abstraction for storage operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

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
        lock_dir: str | None = None,
    ) -> VersionToken:
        """Write file if its version matches expected_version (optimistic concurrency).

        Args:
            path: Backend-relative path to the file.
            data: New file contents.
            expected_version: Version token from the last read.
            lock_dir: Backend-relative directory where the ``.lock`` sidecar file
                should be created.  Callers should pass the ``METADATA_DIR``
                subdirectory for the relevant partition so that lock files are
                kept out of the user's photo folders.  When ``None`` the lock
                file is placed next to the target file.

        Returns:
            New version token after successful write.

        Raises:
            VersionConflictError: If the file's version doesn't match expected_version.
            FileNotFoundError: If the file does not exist.
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
    backend_type = config.get("type")

    if backend_type == "filesystem":
        from .backends.local import LocalBackend

        root = config.get("root")
        if not root:
            raise ConfigurationError("filesystem backend requires 'root' field")
        return LocalBackend(root)

    # Future backends: s3, gcs, adls2, onedrive, kdrive
    raise ConfigurationError(f"Unsupported backend type: {backend_type}")
