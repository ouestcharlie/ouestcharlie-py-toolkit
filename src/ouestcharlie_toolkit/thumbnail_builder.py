"""Thumbnail generation — decode, fit, and assemble photos into AVIF grids.

Pipeline per partition (avif_grid command):
  1. Sort photos by content_hash for stable tile indices
  2. Stage original photo bytes once to a shared local temp directory
  3. Call the image-proc Rust CLI for the requested tier, which decodes +
     resizes + fits + assembles using the staged files
  4. Write the resulting AVIF to the backend
  5. Return (ThumbnailGridLayout, content_hash) for manifest update

Individual JPEG preview generation (jpeg_preview command):
  1. Stage one photo to a temp file
  2. Call image-proc with jpeg_preview to decode + orient + resize → JPEG
  3. Write the JPEG to the backend cache path
  4. Return the backend-relative cache path
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

from ouestcharlie_toolkit.backend import Backend
from ouestcharlie_toolkit.hashing import content_hash as _hash
from ouestcharlie_toolkit.schema import (
    PhotoEntry,
    ThumbnailChunk,
    ThumbnailGridLayout,
    preview_jpeg_path,
    thumbnail_avif_path,
)

_log = logging.getLogger(__name__)

# AVIF encoding quality per tier.
AVIF_QUALITY: dict[str, int] = {"thumbnail": 55, "preview": 60}

# Short-edge pixel sizes per tier.
TILE_SIZES: dict[str, int] = {"thumbnail": 256, "preview": 1440}

# How to fit a non-square photo into a square tile:
#   "crop" — center-crop (natural for small thumbnails)
#   "pad"  — letterbox/pillarbox with black (preserves all content)
TILE_FIT: dict[str, str] = {"thumbnail": "crop", "preview": "pad"}

# Maximum photos per thumbnail AVIF grid chunk (8×8 = 64).
GRID_MAX_PHOTOS: int = 64

# JPEG preview settings.
PREVIEW_JPEG_MAX_LONG_EDGE: int = 1440
PREVIEW_JPEG_QUALITY: int = 85


def _find_image_proc_binary() -> str:
    """Return the path to the image-proc binary.

    Resolution order:
    1. IMAGE_PROC_BINARY environment variable
    2. Bundled binary inside the installed wheel (bin/image-proc[.exe])
    3. image-proc on $PATH (shutil.which)
    4. ../../image-proc/target/release/image-proc relative to this file (dev build)
       i.e. ouestcharlie-py-toolkit/image-proc/target/release/image-proc
    """

    env_bin = os.environ.get("IMAGE_PROC_BINARY")
    if env_bin:
        return env_bin

    # Bundled binary shipped inside the wheel alongside this package.
    binary_name = "image-proc.exe" if sys.platform == "win32" else "image-proc"
    bundled = Path(__file__).parent / "bin" / binary_name
    if bundled.exists():
        return str(bundled)

    on_path = shutil.which("image-proc")
    if on_path:
        return on_path

    # __file__ is at ouestcharlie-py-toolkit/src/ouestcharlie_toolkit/thumbnail_builder.py
    # Three parents up reaches ouestcharlie-py-toolkit/.
    dev_bin = (
        Path(__file__).parent.parent.parent / "image-proc" / "target" / "release" / "image-proc"
    )
    if dev_bin.exists():
        return str(dev_bin)

    raise FileNotFoundError(
        "image-proc binary not found. "
        "Build it with `cargo build --release` inside ouestcharlie-py-toolkit/image-proc/, "
        "or set IMAGE_PROC_BINARY=/path/to/image-proc."
    )


async def _stage_photos(
    backend: Backend,
    partition: str,
    photo_entries: list[PhotoEntry],
    tmpdir: str,
) -> list[dict[str, object]]:
    """Read photos from the backend once and write them to ``tmpdir``.

    Returns the image-proc ``photos`` payload (list of dicts with path, ext,
    orientation, content_hash).  ``photo_entries`` must already be sorted by
    content_hash (caller's responsibility).
    """
    prefix = partition.rstrip("/") + "/" if partition else ""
    photos_payload: list[dict[str, object]] = []
    for i, entry in enumerate(photo_entries):
        photo_path = f"{prefix}{entry.filename}"
        photo_bytes, _ = await backend.read(photo_path)
        ext = os.path.splitext(entry.filename)[1]
        staged_path = os.path.join(tmpdir, f"photo_{i:06d}{ext}")
        Path(staged_path).write_bytes(photo_bytes)
        photos_payload.append(
            {
                "path": staged_path,
                "ext": ext,
                "orientation": entry.searchable.get("orientation"),
                "content_hash": entry.content_hash,
            }
        )
    return photos_payload


async def _call_image_proc(
    staged_photos: list[dict[str, object]],
    tile_size: int,
    fit: str,
    quality: int,
    tmpdir: str,
    binary: str,
) -> tuple[ThumbnailGridLayout, bytes]:
    """Call image-proc (avif_grid command) with pre-staged photos.

    Returns (ThumbnailGridLayout, avif_bytes).  The caller is responsible for
    hashing the bytes, naming the output file, and writing to the backend.
    """
    tmp_output = os.path.join(tmpdir, f"output_{tile_size}.avif")
    payload = json.dumps(
        {
            "photos": staged_photos,
            "tile_size": tile_size,
            "fit": fit,
            "quality": quality,
            "output": tmp_output,
        }
    ).encode()

    proc = await asyncio.create_subprocess_exec(
        binary,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(payload)

    if proc.returncode != 0:
        raise RuntimeError(f"image-proc exited {proc.returncode}: {stderr.decode().strip()}")

    grid_info = json.loads(stdout.decode())
    avif_bytes = Path(tmp_output).read_bytes()
    grid = ThumbnailGridLayout(
        cols=grid_info["cols"],
        rows=grid_info["rows"],
        tile_size=grid_info["tileSize"],
        photo_order=grid_info["photoOrder"],
    )
    return grid, avif_bytes


async def generate_partition_thumbnails(
    backend: Backend,
    partition: str,
    photo_entries: list[PhotoEntry],
    tier: str = "thumbnail",
) -> list[ThumbnailChunk]:
    """Generate AVIF thumbnail chunks for a partition.

    Photos are sorted by ``content_hash`` for stable tile indices, then split
    into chunks of at most ``GRID_MAX_PHOTOS`` (64) entries each, producing a
    maximum 8×8 grid per AVIF file.  Chunks are encoded in parallel.

    Each AVIF file is named ``thumbnails-{avif_hash}.avif`` (or
    ``previews-{avif_hash}.avif`` for the preview tier), where ``avif_hash``
    is the 22-char BLAKE3 hash of the file's content.

    Args:
        tier: ``"thumbnail"`` (256 px, crop) or ``"preview"`` (1440 px, pad).

    Returns:
        List of ``ThumbnailChunk`` in chunk order (sorted by first photo hash).
    """
    binary = _find_image_proc_binary()
    ordered = sorted(photo_entries, key=lambda e: e.content_hash)
    chunks = [ordered[i : i + GRID_MAX_PHOTOS] for i in range(0, len(ordered), GRID_MAX_PHOTOS)]

    async def _generate_chunk(chunk_entries: list[PhotoEntry]) -> ThumbnailChunk:
        with tempfile.TemporaryDirectory() as tmpdir:
            staged = await _stage_photos(backend, partition, chunk_entries, tmpdir)
            grid, avif_bytes = await _call_image_proc(
                staged_photos=staged,
                tile_size=TILE_SIZES[tier],
                fit=TILE_FIT[tier],
                quality=AVIF_QUALITY[tier],
                tmpdir=tmpdir,
                binary=binary,
            )
        avif_hash = _hash(avif_bytes)
        avif_path = thumbnail_avif_path(partition, avif_hash, tier)
        if await backend.exists(avif_path):
            _, version = await backend.read(avif_path)
            await backend.write_conditional(
                avif_path, avif_bytes, version, avif_path.rsplit("/", 1)[0]
            )
        else:
            await backend.write_new(avif_path, avif_bytes)
        _log.debug(
            "AVIF chunk written: %s (%d bytes, %dx%d grid, %d photos)",
            avif_path,
            len(avif_bytes),
            grid.cols,
            grid.rows,
            len(chunk_entries),
        )
        return ThumbnailChunk(avif_hash=avif_hash, grid=grid)

    return list(await asyncio.gather(*[_generate_chunk(c) for c in chunks]))


class PersistentImageProc:
    """Long-running image-proc coprocessor.

    Keeps a single image-proc subprocess alive across multiple preview
    requests.  Requests are serialised through an asyncio.Lock because
    image-proc is single-threaded (one JSON line in → one JSON line out).

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
            line = (json.dumps(payload) + "\n").encode()
            proc.stdin.write(line)
            await proc.stdin.drain()
            response_line = await proc.stdout.readline()
            if not response_line:
                rc = proc.returncode
                raise RuntimeError(f"image-proc closed stdout unexpectedly (exit code {rc})")
            result = json.loads(response_line.decode())
            if "error" in result:
                raise RuntimeError(f"image-proc error: {result['error']}")
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


async def generate_preview_jpeg(
    backend: Backend,
    partition: str,
    entry: PhotoEntry,
    max_long_edge: int = PREVIEW_JPEG_MAX_LONG_EDGE,
    jpeg_quality: int = PREVIEW_JPEG_QUALITY,
    image_proc: PersistentImageProc | None = None,
) -> str:
    """Generate a JPEG preview for a single photo.

    Decodes the original photo via image-proc (handles RAW, JPEG, TIFF, PNG,
    WebP), applies EXIF orientation, resizes to ``max_long_edge`` on the long
    edge (preserving aspect ratio), and saves as JPEG.

    The result is cached at ``.ouestcharlie/{partition}/previews/{content_hash}.jpg``.
    Subsequent calls for the same photo return immediately if the cache file
    already exists on the backend.

    Args:
        backend: Storage backend to read the original photo from and write the JPEG to.
        partition: Partition path relative to backend root (e.g. "2024/2024-07").
        entry: PhotoEntry for the photo (needs content_hash, filename, searchable).
        max_long_edge: Maximum pixel size of the long edge. Default 1440.
        jpeg_quality: JPEG encoding quality 1–95. Default 85.
        image_proc: Optional persistent image-proc instance. When provided, reuses the
            running process instead of spawning a new one. When None, a subprocess is
            spawned per call (backward-compatible behaviour for Whitebeard).

    Returns:
        Backend-relative path of the cached JPEG (e.g.
        ``".ouestcharlie/2024/2024-07/previews/sha256:abc123.jpg"``).
    """
    cache_path = preview_jpeg_path(partition, entry.content_hash)

    # Fast path: already cached.
    if await backend.exists(cache_path):
        return cache_path

    prefix = partition.rstrip("/") + "/" if partition else ""
    photo_path = f"{prefix}{entry.filename}"
    ext = os.path.splitext(entry.filename)[1]

    with tempfile.TemporaryDirectory() as tmpdir:
        # Stage original photo.
        photo_bytes, _ = await backend.read(photo_path)
        staged_path = os.path.join(tmpdir, f"photo{ext}")
        Path(staged_path).write_bytes(photo_bytes)

        tmp_output = os.path.join(tmpdir, "preview.jpg")
        payload = {
            "photo": {
                "path": staged_path,
                "ext": ext,
                "orientation": entry.searchable.get("orientation"),
                "content_hash": entry.content_hash,
            },
            "max_long_edge": max_long_edge,
            "quality": jpeg_quality,
            "output": tmp_output,
        }

        if image_proc is not None:
            result_info = await image_proc.request(payload)
        else:
            binary = _find_image_proc_binary()
            proc = await asyncio.create_subprocess_exec(
                binary,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate(json.dumps(payload).encode())
            if proc.returncode != 0:
                raise RuntimeError(
                    f"image-proc jpeg_preview exited {proc.returncode}: {stderr.decode().strip()}"
                )
            result_info = json.loads(stdout.decode())

        jpeg_bytes = Path(tmp_output).read_bytes()

    # Write to backend (write_new since we checked exists above).
    await backend.write_new(cache_path, jpeg_bytes)

    _log.debug(
        "JPEG preview written: %s (%d bytes, %dx%d)",
        cache_path,
        len(jpeg_bytes),
        result_info["width"],
        result_info["height"],
    )
    return cache_path
