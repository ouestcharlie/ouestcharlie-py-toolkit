"""Thumbnail generation — decode, fit, and assemble photos into AVIF grids.

Pipeline per partition (avif_grid command):
  1. Sort photos by content_hash for stable tile indices
  2. Stage original photo bytes once to a shared local temp directory
  3. For both tiers (thumbnail, preview) in parallel: call the image-proc Rust
     CLI, which decodes + resizes + fits + assembles using the staged files
  4. Write each resulting AVIF to the backend
  5. Return ThumbnailGridLayout + content hashes for manifest update

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
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ouestcharlie_toolkit.backend import Backend
from ouestcharlie_toolkit.hashing import content_hash as _hash
from ouestcharlie_toolkit.schema import METADATA_DIR, ThumbnailGridLayout, PhotoEntry

_log = logging.getLogger(__name__)

# AVIF encoding quality per tier.
AVIF_QUALITY: dict[str, int] = {"thumbnail": 55, "preview": 60}

# Short-edge pixel sizes per tier.
TILE_SIZES: dict[str, int] = {"thumbnail": 256, "preview": 1440}

# How to fit a non-square photo into a square tile:
#   "crop" — center-crop (natural for small thumbnails)
#   "pad"  — letterbox/pillarbox with black (preserves all content)
TILE_FIT: dict[str, str] = {"thumbnail": "crop", "preview": "pad"}

# JPEG preview settings.
PREVIEW_JPEG_MAX_LONG_EDGE: int = 1440
PREVIEW_JPEG_QUALITY: int = 85

# Metadata subdirectory inside .ouestcharlie/ for per-photo JPEG previews.
PREVIEW_JPEG_SUBDIR: str = "previews"


@dataclass
class ThumbnailResult:
    """Result of thumbnail generation for one partition."""

    thumbnails_hash: str
    previews_hash: str
    thumbnail_grid: ThumbnailGridLayout
    preview_grid: ThumbnailGridLayout


def _avif_path(partition: str, tier: str) -> str:
    """Relative backend path for an AVIF container (thumbnails.avif or previews.avif)."""
    prefix = partition.rstrip("/") + "/" if partition else ""
    filename = "thumbnails.avif" if tier == "thumbnail" else "previews.avif"
    return f"{prefix}{METADATA_DIR}/{filename}"


def _preview_jpeg_path(partition: str, content_hash: str) -> str:
    """Relative backend path for a per-photo JPEG preview.

    Example: "2024/2024-07/.ouestcharlie/previews/sha256:abc123.jpg"
    """
    prefix = partition.rstrip("/") + "/" if partition else ""
    return f"{prefix}{METADATA_DIR}/{PREVIEW_JPEG_SUBDIR}/{content_hash}.jpg"


def _find_image_proc_binary() -> str:
    """Return the path to the image-proc binary.

    Resolution order:
    1. IMAGE_PROC_BINARY environment variable
    2. AVIF_GRID_BINARY environment variable (legacy alias)
    3. image-proc on $PATH (shutil.which)
    4. ../../image-proc/target/release/image-proc relative to this file (dev build)
       i.e. ouestcharlie-py-toolkit/image-proc/target/release/image-proc
    """
    env_bin = os.environ.get("IMAGE_PROC_BINARY") or os.environ.get("AVIF_GRID_BINARY")
    if env_bin:
        return env_bin

    on_path = shutil.which("image-proc")
    if on_path:
        return on_path

    # __file__ is at ouestcharlie-py-toolkit/src/ouestcharlie_toolkit/thumbnail_builder.py
    # Three parents up reaches ouestcharlie-py-toolkit/.
    dev_bin = Path(__file__).parent.parent.parent / "image-proc" / "target" / "release" / "image-proc"
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
    photo_entries: list,
    tmpdir: str,
) -> list[dict]:
    """Read photos from the backend once and write them to ``tmpdir``.

    Returns the image-proc ``photos`` payload (list of dicts with path, ext,
    orientation, content_hash).  ``photo_entries`` must already be sorted by
    content_hash (caller's responsibility).
    """
    prefix = partition.rstrip("/") + "/" if partition else ""
    photos_payload: list[dict] = []
    for i, entry in enumerate(photo_entries):
        photo_path = f"{prefix}{entry.filename}"
        photo_bytes, _ = await backend.read(photo_path)
        ext = os.path.splitext(entry.filename)[1]
        staged_path = os.path.join(tmpdir, f"photo_{i:06d}{ext}")
        Path(staged_path).write_bytes(photo_bytes)
        photos_payload.append({
            "path": staged_path,
            "ext": ext,
            "orientation": entry.searchable.get("orientation"),
            "content_hash": entry.content_hash,
        })
    return photos_payload


async def _call_image_proc(
    backend: Backend,
    staged_photos: list[dict],
    tile_size: int,
    fit: str,
    quality: int,
    output_path: str,
    tmpdir: str,
    binary: str,
) -> tuple[ThumbnailGridLayout, str]:
    """Call image-proc (avif_grid command) with pre-staged photos and write the AVIF to the backend.

    Returns (ThumbnailGridLayout, sha256_hash_of_avif).
    """
    tmp_output = os.path.join(tmpdir, f"output_{tile_size}.avif")
    payload = json.dumps({
        "photos": staged_photos,
        "tile_size": tile_size,
        "fit": fit,
        "quality": quality,
        "output": tmp_output,
    }).encode()

    proc = await asyncio.create_subprocess_exec(
        binary,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(payload)

    if proc.returncode != 0:
        raise RuntimeError(
            f"image-proc exited {proc.returncode}: {stderr.decode().strip()}"
        )

    grid_info = json.loads(stdout.decode())
    avif_bytes = Path(tmp_output).read_bytes()

    content_hash = _hash(avif_bytes)

    # Write AVIF to backend (overwrite if already exists).
    if await backend.exists(output_path):
        _, version = await backend.read(output_path)
        await backend.write_conditional(output_path, avif_bytes, version)
    else:
        await backend.write_new(output_path, avif_bytes)

    grid = ThumbnailGridLayout(
        cols=grid_info["cols"],
        rows=grid_info["rows"],
        tile_size=grid_info["tileSize"],
        photo_order=grid_info["photoOrder"],
    )
    _log.debug(
        "AVIF written: %s (%d bytes, %dx%d grid)",
        output_path, len(avif_bytes), grid.cols, grid.rows,
    )
    return grid, content_hash


async def generate_partition_thumbnails(
    backend: Backend,
    partition: str,
    photo_entries: list[PhotoEntry],
    tiers: list[str] | None = None,
) -> ThumbnailResult:
    """Generate thumbnail and/or preview AVIF containers for a partition.

    ``photo_entries`` must have ``.content_hash``, ``.filename``, and
    ``.searchable`` attributes (i.e. ``PhotoEntry`` instances).

    Tile order is deterministic: sorted ascending by ``content_hash``.
    This ensures that a photo's tile index only changes when its content
    changes, not when it is renamed or when unrelated photos are added.

    Args:
        tiers: Which tiers to generate. Defaults to both ``["thumbnail", "preview"]``.
               Pass ``["thumbnail"]`` to skip the preview AVIF container
               (previews are generated lazily as individual JPEGs instead).
    """
    if tiers is None:
        tiers = ["thumbnail", "preview"]

    binary = _find_image_proc_binary()

    # Sort by content_hash for stable tile indices.
    ordered = sorted(photo_entries, key=lambda e: e.content_hash)

    with tempfile.TemporaryDirectory() as tmpdir:
        # Stage photos once; both tiers reuse the same files.
        staged_photos = await _stage_photos(backend, partition, ordered, tmpdir)

        # Encode requested tiers in parallel.
        tier_results: list[tuple[ThumbnailGridLayout, str]] = await asyncio.gather(
            *[
                _call_image_proc(
                    backend=backend,
                    staged_photos=staged_photos,
                    tile_size=TILE_SIZES[tier],
                    fit=TILE_FIT[tier],
                    quality=AVIF_QUALITY[tier],
                    output_path=_avif_path(partition, tier),
                    tmpdir=tmpdir,
                    binary=binary,
                )
                for tier in tiers
            ]
        )
        results = dict(zip(tiers, tier_results))

    thumbnail_grid, thumbnails_hash = results.get("thumbnail", (None, None))
    preview_grid, previews_hash = results.get("preview", (None, None))

    return ThumbnailResult(
        thumbnails_hash=thumbnails_hash or "",
        previews_hash=previews_hash or "",
        thumbnail_grid=thumbnail_grid,
        preview_grid=preview_grid,
    )


async def generate_preview_jpeg(
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

    The result is cached at ``{partition}/.ouestcharlie/previews/{content_hash}.jpg``.
    Subsequent calls for the same photo return immediately if the cache file
    already exists on the backend.

    Args:
        backend: Storage backend to read the original photo from and write the JPEG to.
        partition: Partition path relative to backend root (e.g. "2024/2024-07").
        entry: PhotoEntry for the photo (needs content_hash, filename, searchable).
        max_long_edge: Maximum pixel size of the long edge. Default 1440.
        jpeg_quality: JPEG encoding quality 1–95. Default 85.

    Returns:
        Backend-relative path of the cached JPEG (e.g.
        ``"2024/2024-07/.ouestcharlie/previews/sha256:abc123.jpg"``).
    """
    binary = _find_image_proc_binary()
    cache_path = _preview_jpeg_path(partition, entry.content_hash)

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
        payload = json.dumps({
            "photo": {
                "path": staged_path,
                "ext": ext,
                "orientation": entry.searchable.get("orientation"),
                "content_hash": entry.content_hash,
            },
            "max_long_edge": max_long_edge,
            "quality": jpeg_quality,
            "output": tmp_output,
        }).encode()

        proc = await asyncio.create_subprocess_exec(
            binary,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate(payload)

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
        cache_path, len(jpeg_bytes), result_info["width"], result_info["height"],
    )
    return cache_path
