"""JPEG preview generation for individual photos.

Pipeline (jpeg_preview command):
  1. Stage one photo to a temp file
  2. Call image-proc (persistent or one-shot) to decode + orient + resize → JPEG
  3. Write the JPEG to the backend cache path
  4. Return the backend-relative cache path
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path

from ouestcharlie_toolkit.backend import Backend
from ouestcharlie_toolkit.image_proc import PersistentImageProc, _find_image_proc_binary
from ouestcharlie_toolkit.schema import PhotoEntry, preview_jpeg_path

_log = logging.getLogger(__name__)

# JPEG preview settings.
PREVIEW_JPEG_MAX_LONG_EDGE: int = 1440
PREVIEW_JPEG_QUALITY: int = 85


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
