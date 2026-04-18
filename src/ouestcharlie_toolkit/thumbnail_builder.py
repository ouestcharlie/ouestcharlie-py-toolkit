"""Thumbnail generation — decode, fit, and assemble photos into AVIF grids.

Pipeline per partition (avif_grid command):
  1. Sort photos by content_hash for stable tile indices
  2. Stage original photo bytes once to a shared local temp directory
  3. Call the image-proc Rust CLI for the requested tier, which decodes +
     resizes + fits + assembles using the staged files
  4. Write the resulting AVIF to the backend
  5. Return (ThumbnailGridLayout, content_hash) for manifest update

For individual JPEG preview generation see ``preview_builder``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path

from ouestcharlie_toolkit.backend import Backend
from ouestcharlie_toolkit.hashing import content_hash as _hash
from ouestcharlie_toolkit.image_proc import OneTimeImageProc
from ouestcharlie_toolkit.schema import (
    PhotoEntry,
    ThumbnailChunk,
    ThumbnailGridLayout,
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
) -> tuple[ThumbnailGridLayout, bytes]:
    """Call image-proc (avif_grid command) with pre-staged photos.

    Returns (ThumbnailGridLayout, avif_bytes).  The caller is responsible for
    hashing the bytes, naming the output file, and writing to the backend.
    """
    tmp_output = os.path.join(tmpdir, f"output_{tile_size}.avif")
    grid_info = await OneTimeImageProc().request(
        {
            "photos": staged_photos,
            "tile_size": tile_size,
            "fit": fit,
            "quality": quality,
            "output": tmp_output,
        }
    )
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
