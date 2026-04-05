"""Local filesystem backend implementation."""

from __future__ import annotations

import asyncio
import fcntl
import threading
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
        # Per-path threading locks for intra-process thread safety.
        # flock alone is per-process on macOS/BSD and does not serialize threads.
        self._thread_locks: dict[str, threading.Lock] = {}
        self._thread_locks_mutex = threading.Lock()
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
        """Read file contents and its mtime version token.

        Uses ``fstat()`` on the open file descriptor so that the version token
        is guaranteed to correspond to exactly the bytes that were read.  If
        ``read_bytes`` and ``stat`` were separate calls (with an asyncio
        ``await`` in between), a concurrent writer could replace the file
        between the two calls, producing a content/version mismatch that breaks
        optimistic concurrency.
        """
        full_path = self._resolve(path)

        def _read_with_fstat() -> tuple[bytes, int]:
            import os

            with open(full_path, "rb") as fd:
                mtime_ns = os.fstat(fd.fileno()).st_mtime_ns
                data = fd.read()
            return data, mtime_ns

        loop = asyncio.get_event_loop()
        data, mtime_ns = await loop.run_in_executor(None, _read_with_fstat)
        return data, VersionToken(mtime_ns)

    def _get_thread_lock(self, key: str) -> threading.Lock:
        with self._thread_locks_mutex:
            if key not in self._thread_locks:
                self._thread_locks[key] = threading.Lock()
            return self._thread_locks[key]

    async def write_conditional(
        self, path: str, data: bytes, expected_version: VersionToken
    ) -> VersionToken:
        """Write file using atomic rename, checking mtime version first.

        Holds both a per-path ``threading.Lock`` (intra-process thread safety)
        and an exclusive ``fcntl.flock`` on a ``<path>.lock`` sidecar
        (cross-process safety) for the duration of stat-check + write.

        ``flock`` alone is per-process on macOS/BSD so it does not serialize
        threads within the same process — the threading lock fills that gap.
        """
        full_path = self._resolve(path)
        tmp_path = full_path.with_suffix(full_path.suffix + ".tmp")
        lock_path = full_path.with_suffix(full_path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        thread_lock = self._get_thread_lock(path)

        def _locked_check_and_write() -> int:
            with thread_lock, open(lock_path, "a") as lock_fd:  # noqa: SIM115
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                try:
                    current_mtime = full_path.stat().st_mtime_ns
                    if current_mtime != expected_version.value:
                        raise VersionConflictError(
                            path, expected_version, VersionToken(current_mtime)
                        )
                    tmp_path.write_bytes(data)
                    tmp_path.replace(full_path)
                    return full_path.stat().st_mtime_ns
                finally:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)

        loop = asyncio.get_event_loop()
        new_mtime = await loop.run_in_executor(None, _locked_check_and_write)
        return VersionToken(new_mtime)

    async def write_new(self, path: str, data: bytes) -> VersionToken:
        """Write a new file, failing if it already exists.

        Uses O_CREAT|O_EXCL (``'xb'`` mode) so the existence check and the
        file creation are a single atomic OS operation — no race between
        concurrent callers.
        """
        full_path = self._resolve(path)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, lambda: full_path.parent.mkdir(parents=True, exist_ok=True)
        )

        def _create_exclusive() -> int:
            with open(full_path, "xb") as fd:
                fd.write(data)
            return full_path.stat().st_mtime_ns

        mtime = await loop.run_in_executor(None, _create_exclusive)
        return VersionToken(mtime)

    async def list_dirs(self, prefix: str) -> list[str]:
        """List immediate subdirectory paths under prefix."""
        prefix_path = self._resolve(prefix)

        if not prefix_path.exists():
            return []

        return [str(p.relative_to(self.root)) for p in prefix_path.iterdir() if p.is_dir()]

    async def list_files(
        self,
        prefix: str,
        suffixes: frozenset[str] | None = None,
    ) -> list[FileInfo]:
        """List direct-child files under prefix, optionally filtered by extension."""
        prefix_path = self._resolve(prefix)

        if not prefix_path.exists():
            return []

        if not prefix_path.is_dir():
            raise NotADirectoryError(f"Prefix is not a directory: {prefix}")

        loop = asyncio.get_event_loop()

        # Build one glob pattern per suffix (or a single catch-all).
        # Non-recursive patterns (*ext) let the OS skip subdirectories entirely.
        patterns = [f"*{s}" for s in suffixes] if suffixes else ["*"]

        def _list_files() -> list[FileInfo]:
            results = []
            for pattern in patterns:
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
