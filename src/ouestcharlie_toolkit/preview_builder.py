"""JPEG preview generation for individual photos.

Pipeline (jpeg_preview command):
  1. Stage one photo to a temp file
  2. Call image-proc (persistent or one-shot) to decode + orient + resize → JPEG
  3. Write the JPEG to the backend cache path
  4. Return the backend-relative cache path
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from ouestcharlie_toolkit.backend import Backend
from ouestcharlie_toolkit.image_proc import PersistentImageProc
from ouestcharlie_toolkit.schema import PhotoEntry, preview_jpeg_path

_log = logging.getLogger(__name__)

# JPEG preview settings.
PREVIEW_JPEG_MAX_LONG_EDGE: int = 1440
PREVIEW_JPEG_QUALITY: int = 85


async def generate_preview_jpeg(
    image_proc: PersistentImageProc,
    backend: Backend,
    partition: str,
    entry: PhotoEntry,
    max_long_edge: int = PREVIEW_JPEG_MAX_LONG_EDGE,
    jpeg_quality: int = PREVIEW_JPEG_QUALITY,
) -> str:
    """Generate a JPEG preview for a single photo.

    Decodes the original photo via image-proc (handles RAW, JPEG, TIFF, PNG,
    WebP), applies EXIF orientation, resizes to ``max_long_edge`` on the long
    edge (preserving aspect ratio), and saves as JPEG.

    The result is cached at ``.ouestcharlie/{partition}/previews/{content_hash}.jpg``.
    Subsequent calls for the same photo return immediately if the cache file
    already exists on the backend.

    Args:
        image_proc: Persistent image-proc instance to use for decoding and resizing.
        backend: Storage backend to read the original photo from and write the JPEG to.
        partition: Partition path relative to backend root (e.g. "2024/2024-07").
        entry: PhotoEntry for the photo (needs content_hash, filename, searchable).
        max_long_edge: Maximum pixel size of the long edge. Default 1440.
        jpeg_quality: JPEG encoding quality 1–95. Default 85.

    Returns:
        Backend-relative path of the cached JPEG (e.g.
        ``".ouestcharlie/2024/2024-07/previews/sha256:abc123.jpg"``).
    """
    cache_path = preview_jpeg_path(partition, entry.content_hash)

    # Fast path: already cached.
    if await backend.exists(cache_path):
        _log.debug("Preview already cached: hash=%r path=%s", entry.content_hash, cache_path)
        return cache_path

    prefix = partition.rstrip("/") + "/" if partition else ""
    photo_path = f"{prefix}{entry.filename}"
    ext = os.path.splitext(entry.filename)[1]

    _log.info(
        "Preview generation start: hash=%r filename=%r photo_path=%s",
        entry.content_hash,
        entry.filename,
        photo_path,
    )

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
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

        result_info = await image_proc.request(payload)

        _log.debug(
            "image-proc returned: hash=%r dimensions=%dx%d",
            entry.content_hash,
            result_info["width"],
            result_info["height"],
        )
        jpeg_bytes = Path(tmp_output).read_bytes()

    # Write to backend (write_new since we checked exists above).
    _log.info("Writing preview to backend: hash=%r cache_path=%s", entry.content_hash, cache_path)
    await backend.write_new(cache_path, jpeg_bytes)

    _log.debug(
        "Preview written: hash=%r filename=%r cache_path=%s size=%d bytes %dx%d",
        entry.content_hash,
        entry.filename,
        cache_path,
        len(jpeg_bytes),
        result_info["width"],
        result_info["height"],
    )
    return cache_path
