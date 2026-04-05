"""Local filesystem backend implementation."""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

from ..schema import FileInfo, VersionConflictError, VersionToken

# ---------------------------------------------------------------------------
# Platform-specific cross-process locking
# ---------------------------------------------------------------------------
# Two separate class bodies — one per platform — so each branch only
# references imports that are statically available on that platform.
# Pyright evaluates sys.platform checks statically, so this avoids false
# "attribute not found on None" errors from the try/except-import pattern.

if sys.platform == "win32":
    import msvcrt as _msvcrt

    class _CrossProcessLock:
        """Exclusive cross-process lock on a sidecar ``.lock`` file (Windows).

        Uses ``msvcrt.locking(LK_LOCK, 1)`` which spin-waits up to 10 s on
        1 byte at position 0 of the lock file, then raises ``OSError``.
        ``msvcrt.locking`` is per-process, so the caller must also hold a
        ``threading.Lock`` to serialise threads within the same process.
        """

        def __init__(self, lock_path: Path) -> None:
            self._lock_path = lock_path
            self._fd = None

        def __enter__(self) -> _CrossProcessLock:
            self._fd = open(self._lock_path, "a")  # noqa: SIM115
            self._fd.flush()
            _msvcrt.locking(self._fd.fileno(), _msvcrt.LK_LOCK, 1)
            return self

        def __exit__(self, *_: object) -> None:
            self._fd.seek(0)
            _msvcrt.locking(self._fd.fileno(), _msvcrt.LK_UNLCK, 1)
            self._fd.close()
            self._fd = None

else:
    import fcntl as _fcntl
    from typing import IO

    class _CrossProcessLock:  # type: ignore[no-redef]
        """Exclusive cross-process lock on a sidecar ``.lock`` file (POSIX).

        Uses ``fcntl.flock(LOCK_EX)`` held on the open fd.

        - **macOS/BSD:** ``flock`` is per-process and does *not* serialise
          threads within the same process — the caller must also hold a
          ``threading.Lock``.
        - **Linux:** ``flock`` is per open-file-description, so it does
          serialise threads; the ``threading.Lock`` is redundant but harmless.
        """

        def __init__(self, lock_path: Path) -> None:
            self._lock_path = lock_path
            self._fd: IO[str] | None = None

        def __enter__(self) -> _CrossProcessLock:
            self._fd = open(self._lock_path, "a")  # noqa: SIM115
            _fcntl.flock(self._fd, _fcntl.LOCK_EX)
            return self

        def __exit__(self, *_: object) -> None:
            assert self._fd is not None
            _fcntl.flock(self._fd, _fcntl.LOCK_UN)
            self._fd.close()
            self._fd = None


class LocalBackend:
    """Local filesystem backend using pathlib for file operations."""

    def __init__(self, root: str | Path) -> None:
        """Initialize the local backend.

        Args:
            root: Root directory path for this backend.
        """
        self.root = Path(root).resolve()
        # Per-path threading locks for intra-process thread safety.
        # On macOS/BSD, flock alone is per-process and does not serialise
        # threads within the same process.  On Windows, msvcrt.locking is
        # similarly per-process.  The threading lock fills that gap on both.
        self._thread_locks: dict[str, threading.Lock] = {}
        self._thread_locks_mutex = threading.Lock()
        if not self.root.exists():
            raise FileNotFoundError(f"Backend root does not exist: {self.root}")
        if not self.root.is_dir():
            raise NotADirectoryError(f"Backend root is not a directory: {self.root}")

    def _resolve(self, path: str) -> Path:
        """Resolve a relative path to an absolute path within the root."""
        full_path = (self.root / path).resolve()
        # Security check: ensure the resolved path is within root.
        # Use is_relative_to rather than str.startswith so that case
        # differences on Windows (case-insensitive filesystem) are handled
        # correctly by pathlib.
        if not full_path.is_relative_to(self.root):
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
        and an exclusive ``_CrossProcessLock`` on a ``<path>.lock`` sidecar
        (cross-process safety) for the duration of stat-check + write.

        On macOS/BSD, ``flock`` is per-process and does not serialise threads
        within the same process — the threading lock fills that gap.
        On Windows, ``msvcrt.locking`` is similarly per-process.
        """
        full_path = self._resolve(path)
        tmp_path = full_path.with_suffix(full_path.suffix + ".tmp")
        lock_path = full_path.with_suffix(full_path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        thread_lock = self._get_thread_lock(path)

        def _locked_check_and_write() -> int:
            with thread_lock, _CrossProcessLock(lock_path):
                current_mtime = full_path.stat().st_mtime_ns
                if current_mtime != expected_version.value:
                    raise VersionConflictError(path, expected_version, VersionToken(current_mtime))
                tmp_path.write_bytes(data)
                tmp_path.replace(full_path)
                return full_path.stat().st_mtime_ns

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

        return [p.relative_to(self.root).as_posix() for p in prefix_path.iterdir() if p.is_dir()]

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
                                path=relative.as_posix(),
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
