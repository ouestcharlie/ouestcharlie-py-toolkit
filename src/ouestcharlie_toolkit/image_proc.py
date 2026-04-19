"""image-proc coprocessor — binary discovery and subprocess wrappers.

Provides:
  - ``_find_image_proc_binary()``: locates the image-proc Rust binary.
  - ``OneTimeImageProc``: spawns a fresh process per request (suitable for
    batch workloads where requests are already parallelised at a higher level).
  - ``PersistentImageProc``: keeps one image-proc process alive across requests,
    communicating via newline-delimited JSON on stdin/stdout.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
from pathlib import Path

_log = logging.getLogger(__name__)

# Major version of the Python↔image-proc JSON protocol.
# Must match the major component of the Cargo.toml version in image-proc/.
# Bump both together whenever the protocol changes in a breaking way.
IMAGE_PROC_PROTOCOL_MAJOR_VERSION = 1


def _find_image_proc_binary() -> str:
    """Return the path to the image-proc binary.

    Resolution order:
    1. IMAGE_PROC_BINARY environment variable
    2. Bundled binary inside the installed wheel (bin/image-proc[.exe])
    """
    env_bin = os.environ.get("IMAGE_PROC_BINARY")
    if env_bin:
        return env_bin

    binary_name = "image-proc.exe" if sys.platform == "win32" else "image-proc"
    bundled = Path(__file__).parent / "bin" / binary_name
    if bundled.exists():
        return str(bundled)

    raise FileNotFoundError(
        "image-proc binary not found. "
        "Build it with `cargo build --release` inside ouestcharlie-py-toolkit/image-proc/, "
        "or set IMAGE_PROC_BINARY=/path/to/image-proc."
    )


class OneTimeImageProc:
    """Spawns a fresh image-proc process for each request.

    Each ``request()`` call starts a new process, sends the JSON payload on
    stdin, waits for the process to exit, and returns the parsed response.

    Suitable for batch workloads (e.g. AVIF grid assembly) where individual
    requests are already parallelised at a higher level and there is no
    long-lived session to share a process across.  No cleanup is required.

    Usage::

        proc = OneTimeImageProc()
        result = await proc.request({"photos": [...], "tile_size": 256, ...})
    """

    def __init__(self, binary: str | None = None) -> None:
        self._binary = binary  # resolved lazily so FileNotFoundError surfaces at request time

    async def request(self, payload: dict) -> dict:
        """Send one JSON request in a fresh process and return the parsed response.

        Raises ``RuntimeError`` if image-proc exits with a non-zero code or
        returns an error object.
        """
        if self._binary is None:
            self._binary = _find_image_proc_binary()
        proc = await asyncio.create_subprocess_exec(
            self._binary,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        payload = {**payload, "protocol_version": IMAGE_PROC_PROTOCOL_MAJOR_VERSION}
        line = (json.dumps(payload) + "\n").encode()
        stdout, stderr = await proc.communicate(line)
        if proc.returncode != 0:
            raise RuntimeError(f"image-proc exited {proc.returncode}: {stderr.decode().strip()}")
        result = json.loads(stdout.decode())
        if "error" in result:
            raise RuntimeError(f"image-proc error: {result['error']}")
        return result


class PersistentImageProc:
    """Long-running image-proc coprocessor.

    Keeps a single image-proc subprocess alive across multiple requests.
    Requests are serialised through an asyncio.Lock because image-proc is
    single-threaded (one JSON line in → one JSON line out).

    The process is spawned lazily on the first request and restarted
    automatically if it crashes.

    Usage::

        proc = PersistentImageProc()
        result = await proc.request({"photo": ..., "max_long_edge": 1440, ...})
        await proc.close()

    Or as an async context manager::

        async with PersistentImageProc() as proc:
            result = await proc.request(payload)
    """

    def __init__(self, binary: str | None = None) -> None:
        self._binary = binary  # resolved lazily so FileNotFoundError surfaces at request time
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> PersistentImageProc:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def _ensure_running(self) -> asyncio.subprocess.Process:
        """Return the running process, (re)starting it if necessary."""
        if self._proc is not None and self._proc.returncode is None:
            return self._proc
        if self._binary is None:
            self._binary = _find_image_proc_binary()
        _log.debug("Starting persistent image-proc: %s", self._binary)
        self._proc = await asyncio.create_subprocess_exec(
            self._binary,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return self._proc

    async def request(self, payload: dict) -> dict:
        """Send one JSON request and return the parsed JSON response.

        Raises ``RuntimeError`` if image-proc returns an error object or if
        the process dies unexpectedly.
        """
        async with self._lock:
            proc = await self._ensure_running()
            assert proc.stdin is not None and proc.stdout is not None
            output_path = payload.get("output", "<unknown>")
            _log.debug(
                "PersistentImageProc send: pid=%d output=%s",
                proc.pid,
                output_path,
            )
            payload = {**payload, "protocol_version": IMAGE_PROC_PROTOCOL_MAJOR_VERSION}
            line = (json.dumps(payload) + "\n").encode()
            proc.stdin.write(line)
            await proc.stdin.drain()
            _log.debug(
                "PersistentImageProc waiting for response: pid=%d output=%s", proc.pid, output_path
            )
            response_line = await proc.stdout.readline()
            if not response_line:
                rc = proc.returncode
                raise RuntimeError(f"image-proc closed stdout unexpectedly (exit code {rc})")
            result = json.loads(response_line.decode())
            if "error" in result:
                _log.error(
                    "PersistentImageProc error: pid=%d output=%s error=%r",
                    proc.pid,
                    output_path,
                    result["error"],
                )
                raise RuntimeError(f"image-proc error: {result['error']}")
            _log.debug(
                "PersistentImageProc response received: pid=%d output=%s result=%s",
                proc.pid,
                output_path,
                result,
            )
            return result

    async def close(self) -> None:
        """Shut down the image-proc process gracefully."""
        if self._proc is None:
            return
        proc = self._proc
        self._proc = None
        if proc.stdin and not proc.stdin.is_closing():
            proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except TimeoutError:
            _log.warning("image-proc did not exit cleanly; terminating")
            proc.terminate()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            if proc.returncode is None:
                _log.warning("image-proc did not exit after SIGTERM; killing")
                proc.kill()
