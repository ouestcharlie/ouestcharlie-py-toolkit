"""Backend abstraction for storage operations."""

from __future__ import annotations

from typing import Protocol

from .schema import FileInfo, VersionToken


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
        self, path: str, data: bytes, expected_version: VersionToken
    ) -> VersionToken:
        """Write file if its version matches expected_version (optimistic concurrency).

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

    async def list_files(self, prefix: str, suffix: str = "") -> list[FileInfo]:
        """List files under prefix, optionally filtered by suffix.

        Args:
            prefix: Directory/prefix to list files from.
            suffix: Optional suffix filter (e.g., ".xmp", ".jpg").

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


def backend_from_config(config: dict) -> Backend:
    """Factory function to create a Backend instance from configuration.

    Args:
        config: Backend configuration dict with 'type' and type-specific fields.
                Example: {"type": "filesystem", "root": "/path/to/photos"}

    Returns:
        Backend instance matching the configured type.

    Raises:
        ConfigurationError: If config is invalid or backend type is unsupported.
    """
    from .schema import ConfigurationError

    backend_type = config.get("type")

    if backend_type == "filesystem":
        from .backends.local import LocalBackend
        root = config.get("root")
        if not root:
            raise ConfigurationError("filesystem backend requires 'root' field")
        return LocalBackend(root)

    # Future backends: s3, gcs, adls2, onedrive, kdrive
    raise ConfigurationError(f"Unsupported backend type: {backend_type}")
