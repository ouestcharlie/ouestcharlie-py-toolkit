"""CloudMountedBackend — LocalBackend for FUSE/CF-API cloud mounts."""

from __future__ import annotations

import asyncio
import logging
import os

from ..backend import VersionToken
from .local import LocalBackend

_log = logging.getLogger(__name__)

_MAX_RETRIES = 5
_RETRY_BASE_DELAY = 0.2  # seconds


class CloudMountedBackend(LocalBackend):
    """LocalBackend for FUSE/Windows-CF-API cloud mounts (kDrive, OneDrive, GDrive, Dropbox).

    fstat().st_size returns the logical (cloud) size even for dehydrated files, so
    comparing len(data) against st_size reliably detects an incomplete read.  When
    the read is incomplete the file is re-read with exponential backoff to give the
    sync client time to finish the download.  OSError is raised after _MAX_RETRIES.

    Configure with {"type": "cloud_mount", "root": "/path/to/mount"}.
    """

    async def read(self, path: str) -> tuple[bytes, VersionToken]:
        full_path = self._resolve(path)
        loop = asyncio.get_event_loop()

        def _read_inner() -> tuple[bytes, int, int]:
            """Read file bytes + mtime_ns + st_size from a single open fd."""
            with open(full_path, "rb") as fd:
                mtime_ns = os.fstat(fd.fileno()).st_mtime_ns
                data = fd.read()
                st_size = os.fstat(fd.fileno()).st_size
            return data, mtime_ns, st_size

        # Read one to trigger rehydratation
        with open(full_path, "rb") as fd:
            data = fd.read(1)

        delay = _RETRY_BASE_DELAY
        for attempt in range(_MAX_RETRIES + 1):
            data, mtime_ns, st_size = await loop.run_in_executor(None, _read_inner)
            _log.debug("Cloud-mounted file %r: read %d bytes, st_size=%d", path, len(data), st_size)

            if len(data) < st_size:
                if attempt < _MAX_RETRIES:
                    _log.debug(
                        "Incomplete read, retrying in %.1fs (attempt %d/%d): %r",
                        delay,
                        attempt + 1,
                        _MAX_RETRIES,
                        path,
                    )
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                raise OSError(
                    f"Incomplete read for cloud-mounted file after {_MAX_RETRIES} retries"
                    f" (got {len(data)} of {st_size} bytes): {path!r}"
                )

            return data, VersionToken(mtime_ns)

        raise AssertionError("unreachable")  # pragma: no cover
