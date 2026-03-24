"""Local filesystem backend implementation."""

from __future__ import annotations

import asyncio
from pathlib import Path

from ..schema import FileInfo, VersionConflictError, VersionToken


class LocalBackend:
    """Local filesystem backend using pathlib for file operations."""

    def __init__(self, root: str | Path) -> None:
        """Initialize the local backend.

        Args:
            root: Root directory path for this backend.
        """
        self.root = Path(root).resolve()
        if not self.root.exists():
            raise FileNotFoundError(f"Backend root does not exist: {self.root}")
        if not self.root.is_dir():
            raise NotADirectoryError(f"Backend root is not a directory: {self.root}")

    def _resolve(self, path: str) -> Path:
        """Resolve a relative path to an absolute path within the root."""
        full_path = (self.root / path).resolve()
        # Security check: ensure the resolved path is within root
        if not str(full_path).startswith(str(self.root)):
            raise ValueError(f"Path escapes backend root: {path}")
        return full_path

    async def read(self, path: str) -> tuple[bytes, VersionToken]:
        """Read file contents and its mtime version token."""
        full_path = self._resolve(path)
        # Run blocking I/O in executor
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, full_path.read_bytes)
        stat = await loop.run_in_executor(None, full_path.stat)
        return data, VersionToken(stat.st_mtime_ns)

    async def write_conditional(
        self, path: str, data: bytes, expected_version: VersionToken
    ) -> VersionToken:
        """Write file using atomic rename, checking mtime version first."""
        full_path = self._resolve(path)

        # Check version before write
        loop = asyncio.get_event_loop()
        stat = await loop.run_in_executor(None, full_path.stat)
        current_mtime = stat.st_mtime_ns

        if current_mtime != expected_version.value:
            raise VersionConflictError(path, expected_version, VersionToken(current_mtime))

        # Write to temp file, then atomic rename
        tmp_path = full_path.with_suffix(full_path.suffix + ".tmp")

        await loop.run_in_executor(None, tmp_path.write_bytes, data)
        await loop.run_in_executor(None, tmp_path.replace, full_path)

        # Return new mtime
        new_stat = await loop.run_in_executor(None, full_path.stat)
        return VersionToken(new_stat.st_mtime_ns)

    async def write_new(self, path: str, data: bytes) -> VersionToken:
        """Write a new file, failing if it already exists."""
        full_path = self._resolve(path)

        if full_path.exists():
            raise FileExistsError(f"File already exists: {path}")

        # Ensure parent directory exists (parents=True, exist_ok=True).
        # Note: run_in_executor only accepts positional args, so we use a lambda
        # to pass keyword arguments correctly.
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, lambda: full_path.parent.mkdir(parents=True, exist_ok=True)
        )

        # Write the file
        await loop.run_in_executor(None, full_path.write_bytes, data)

        stat = await loop.run_in_executor(None, full_path.stat)
        return VersionToken(stat.st_mtime_ns)

    async def list_files(self, prefix: str, suffix: str = "") -> list[FileInfo]:
        """List files under prefix, optionally filtered by suffix."""
        prefix_path = self._resolve(prefix)

        if not prefix_path.exists():
            return []

        if not prefix_path.is_dir():
            raise NotADirectoryError(f"Prefix is not a directory: {prefix}")

        # Use glob pattern
        pattern = f"**/*{suffix}" if suffix else "**/*"

        loop = asyncio.get_event_loop()

        def _list_files() -> list[FileInfo]:
            results = []
            for file_path in prefix_path.glob(pattern):
                if file_path.is_file():
                    relative = file_path.relative_to(self.root)
                    stat = file_path.stat()
                    results.append(
                        FileInfo(
                            path=str(relative),
                            version=VersionToken(stat.st_mtime_ns),
                        )
                    )
            return results

        return await loop.run_in_executor(None, _list_files)

    async def exists(self, path: str) -> bool:
        """Check if a file exists."""
        full_path = self._resolve(path)
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, full_path.exists)

    async def delete(self, path: str) -> None:
        """Delete a file."""
        full_path = self._resolve(path)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, full_path.unlink)
