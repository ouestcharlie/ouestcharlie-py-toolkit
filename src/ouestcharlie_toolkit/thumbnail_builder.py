"""Thumbnail tile cache management and AVIF grid assembly.

Pipeline per partition:
  1. For each photo: decode+resize to JPEG tile → store in .ouestcharlie/tile_cache/
  2. Sort tiles by content_hash for stable indices
  3. Call the avif-grid Rust CLI to assemble tiles into thumbnails.avif / previews.avif
  4. Return ThumbnailGridLayout + SHA-256 hashes for manifest update
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ouestcharlie_toolkit.backend import Backend
from ouestcharlie_toolkit.schema import METADATA_DIR, ThumbnailGridLayout
from ouestcharlie_toolkit.thumbnail import decode_and_resize

_log = logging.getLogger(__name__)

# Tile quality for JPEG intermediate storage (lossless-enough for re-encoding).
_TILE_JPEG_QUALITY = 95

# AVIF encoding quality per tier.
AVIF_QUALITY: dict[str, int] = {"thumbnail": 55, "preview": 60}

# Short-edge pixel sizes per tier.
TILE_SIZES: dict[str, int] = {"thumbnail": 256, "preview": 1440}

# How to produce a square tile from a non-square photo:
#   "crop" — center-crop to tile_size × tile_size (natural for small thumbnails)
#   "pad"  — letterbox/pillarbox with black to tile_size × tile_size (preserves content)
TILE_FIT: dict[str, str] = {"thumbnail": "crop", "preview": "pad"}


@dataclass
class ThumbnailResult:
    """Result of thumbnail generation for one partition."""

    thumbnails_hash: str
    previews_hash: str
    thumbnail_grid: ThumbnailGridLayout
    preview_grid: ThumbnailGridLayout


def _tile_cache_path(partition: str, content_hash: str, tile_size: int) -> str:
    """Relative backend path for a cached tile JPEG.

    Example: "2024/2024-07/.ouestcharlie/tile_cache/a1b2c3d4e5f60001_256.jpg"
    The hash prefix (first 16 hex chars) is enough for cache keying since it's
    derived from the full SHA-256 content hash.
    """
    prefix = partition.rstrip("/") + "/" if partition else ""
    # content_hash is "sha256:<hex>" — strip the prefix
    hex_part = content_hash.split(":", 1)[-1]
    key = f"{hex_part[:16]}_{tile_size}"
    return f"{prefix}{METADATA_DIR}/tile_cache/{key}.jpg"


def _avif_path(partition: str, tier: str) -> str:
    """Relative backend path for an AVIF container (thumbnails.avif or previews.avif)."""
    prefix = partition.rstrip("/") + "/" if partition else ""
    filename = "thumbnails.avif" if tier == "thumbnail" else "previews.avif"
    return f"{prefix}{METADATA_DIR}/{filename}"


def _find_avif_grid_binary() -> str:
    """Return the path to the avif-grid binary.

    Resolution order:
    1. AVIF_GRID_BINARY environment variable
    2. avif-grid on $PATH (shutil.which)
    3. ../../avif-grid/target/release/avif-grid relative to this file (dev build)
       i.e. ouestcharlie-py-toolkit/avif-grid/target/release/avif-grid
    """
    env_bin = os.environ.get("AVIF_GRID_BINARY")
    if env_bin:
        return env_bin

    on_path = shutil.which("avif-grid")
    if on_path:
        return on_path

    # __file__ is at ouestcharlie-py-toolkit/src/ouestcharlie_toolkit/thumbnail_builder.py
    # Three parents up reaches ouestcharlie-py-toolkit/.
    dev_bin = Path(__file__).parent.parent.parent / "avif-grid" / "target" / "release" / "avif-grid"
    if dev_bin.exists():
        return str(dev_bin)

    raise FileNotFoundError(
        "avif-grid binary not found. "
        "Build it with `cargo build --release` inside ouestcharlie-py-toolkit/avif-grid/, "
        "or set AVIF_GRID_BINARY=/path/to/avif-grid."
    )


def _fit_to_square(img: "Image.Image", size: int, fit: str) -> "Image.Image":
    """Return a ``size × size`` PIL Image fitted according to ``fit``.

    ``fit="crop"``  — center-crop; assumes short_edge == size (from decode_and_resize).
    ``fit="pad"``   — downscale so long_edge == size, then letterbox with black.
    """
    from PIL import Image  # local import to keep module-level deps minimal

    w, h = img.size
    if fit == "crop":
        left = (w - size) // 2
        top = (h - size) // 2
        return img.crop((left, top, left + size, top + size))
    else:  # "pad"
        # Resize so the long edge equals size.
        scale = size / max(w, h)
        if scale < 1.0:
            img = img.resize((round(w * scale), round(h * scale)), Image.Resampling.LANCZOS)
        # Paste centered on a black canvas.
        canvas = Image.new("RGB", (size, size), (0, 0, 0))
        paste_x = (size - img.width) // 2
        paste_y = (size - img.height) // 2
        canvas.paste(img, (paste_x, paste_y))
        return canvas


async def ensure_tile(
    backend: Backend,
    photo_path: str,
    content_hash: str,
    orientation: int | None,
    tile_size: int,
    partition: str,
    fit: str = "crop",
) -> str:
    """Ensure a JPEG tile exists in the tile cache; return its backend-relative path.

    If the tile already exists (cache hit), returns the path immediately.
    Otherwise decodes, resizes, and fits the photo to a square tile, saves it,
    and returns the path.

    ``fit`` controls how non-square photos are squared up:
    - ``"crop"`` — center-crop to ``tile_size × tile_size`` (default; natural for
      small gallery thumbnails where aspect ratio matters less than fill).
    - ``"pad"``  — resize so the long edge equals ``tile_size``, then letterbox /
      pillarbox with black to ``tile_size × tile_size`` (preserves all content).
    """
    tile_path = _tile_cache_path(partition, content_hash, tile_size)

    if await backend.exists(tile_path):
        return tile_path

    # Read photo bytes from backend.
    photo_bytes, _ = await backend.read(photo_path)

    # Decode via Pillow/rawpy using a temp file (storage-agnostic).
    ext = os.path.splitext(photo_path)[1]
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp.write(photo_bytes)
        tmp_path = tmp.name

    try:
        img = await asyncio.to_thread(decode_and_resize, tmp_path, orientation, tile_size)
    finally:
        os.unlink(tmp_path)

    # Square the tile according to the requested fit mode.
    # decode_and_resize guarantees short_edge == tile_size; for uniform AVIF grid
    # tiles every photo must produce exactly tile_size × tile_size pixels.
    img = await asyncio.to_thread(_fit_to_square, img, tile_size, fit)

    buf = io.BytesIO()
    await asyncio.to_thread(img.save, buf, format="JPEG", quality=_TILE_JPEG_QUALITY)
    jpeg_bytes = buf.getvalue()

    await backend.write_new(tile_path, jpeg_bytes)
    _log.debug("Tile cached: %s (%d bytes)", tile_path, len(jpeg_bytes))
    return tile_path


async def assemble_avif(
    backend: Backend,
    tile_paths: list[str],
    quality: int,
    output_path: str,
    avif_grid_binary: str,
) -> tuple[ThumbnailGridLayout, str]:
    """Call the avif-grid CLI to assemble tiles into an AVIF grid container.

    Returns (ThumbnailGridLayout, sha256_hash_of_avif).
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        # Stage all tiles to a local temp directory (works for any Backend).
        tmp_tile_paths: list[str] = []
        for i, rel_path in enumerate(tile_paths):
            tile_bytes, _ = await backend.read(rel_path)
            tmp_path = os.path.join(tmpdir, f"tile_{i:06d}.jpg")
            Path(tmp_path).write_bytes(tile_bytes)
            tmp_tile_paths.append(tmp_path)

        tmp_output = os.path.join(tmpdir, "output.avif")
        payload = json.dumps({
            "tiles": tmp_tile_paths,
            "quality": quality,
            "output": tmp_output,
        }).encode()

        proc = await asyncio.create_subprocess_exec(
            avif_grid_binary,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate(payload)

        if proc.returncode != 0:
            raise RuntimeError(
                f"avif-grid exited {proc.returncode}: {stderr.decode().strip()}"
            )

        grid_info = json.loads(stdout.decode())
        avif_bytes = Path(tmp_output).read_bytes()

    content_hash = "sha256:" + hashlib.sha256(avif_bytes).hexdigest()

    # Write AVIF to backend (overwrite if already exists).
    if await backend.exists(output_path):
        _, version = await backend.read(output_path)
        await backend.write_conditional(output_path, avif_bytes, version)
    else:
        await backend.write_new(output_path, avif_bytes)

    tile_size = grid_info["tileSize"]
    grid = ThumbnailGridLayout(
        cols=grid_info["cols"],
        rows=grid_info["rows"],
        tile_size=tile_size,
        photo_order=[],  # filled in by the caller who knows content_hash order
    )
    return grid, content_hash


async def generate_partition_thumbnails(
    backend: Backend,
    partition: str,
    photo_entries: list,  # list[PhotoEntry] — avoid circular import
) -> ThumbnailResult:
    """Generate thumbnail and preview AVIF containers for a partition.

    ``photo_entries`` must have ``.content_hash``, ``.filename``, and
    ``.orientation`` attributes (i.e. ``PhotoEntry`` instances).

    Tile order is deterministic: sorted ascending by ``content_hash``.
    This ensures that a photo's tile index only changes when its content
    changes, not when it is renamed or when unrelated photos are added.
    """
    avif_grid_binary = _find_avif_grid_binary()

    # Sort by content_hash for stable tile indices.
    ordered = sorted(photo_entries, key=lambda e: e.content_hash)

    results: dict[str, tuple[ThumbnailGridLayout, str]] = {}

    for tier, tile_size in TILE_SIZES.items():
        # Ensure all tiles exist in the cache.
        tile_paths: list[str] = []
        for entry in ordered:
            photo_path = (partition.rstrip("/") + "/" if partition else "") + entry.filename
            tile_path = await ensure_tile(
                backend=backend,
                photo_path=photo_path,
                content_hash=entry.content_hash,
                orientation=entry.orientation,
                tile_size=tile_size,
                partition=partition,
                fit=TILE_FIT[tier],
            )
            tile_paths.append(tile_path)

        output_path = _avif_path(partition, tier)
        quality = AVIF_QUALITY[tier]

        grid, content_hash = await assemble_avif(
            backend=backend,
            tile_paths=tile_paths,
            quality=quality,
            output_path=output_path,
            avif_grid_binary=avif_grid_binary,
        )
        # Record the photo_order (content_hashes in tile order).
        grid.photo_order = [e.content_hash for e in ordered]
        results[tier] = (grid, content_hash)

    thumbnail_grid, thumbnails_hash = results["thumbnail"]
    preview_grid, previews_hash = results["preview"]

    return ThumbnailResult(
        thumbnails_hash=thumbnails_hash,
        previews_hash=previews_hash,
        thumbnail_grid=thumbnail_grid,
        preview_grid=preview_grid,
    )
