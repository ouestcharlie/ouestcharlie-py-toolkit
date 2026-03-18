"""Thumbnail generation — decode, fit, and assemble photos into AVIF grids.

Pipeline per partition:
  1. Sort photos by content_hash for stable tile indices
  2. Stage original photo bytes to a local temp directory
  3. Call the avif-grid Rust CLI, which decodes + resizes + fits + assembles
  4. Write the resulting AVIF to the backend
  5. Return ThumbnailGridLayout + SHA-256 hashes for manifest update
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ouestcharlie_toolkit.backend import Backend
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


async def _call_avif_grid(
    backend: Backend,
    partition: str,
    photo_entries: list,
    tile_size: int,
    fit: str,
    quality: int,
    output_path: str,
    avif_grid_binary: str,
) -> tuple[ThumbnailGridLayout, str]:
    """Stage photos, call the avif-grid CLI, and write the AVIF to the backend.

    The CLI handles decode + resize + fit + AVIF assembly.
    Returns (ThumbnailGridLayout, sha256_hash_of_avif).

    ``photo_entries`` must already be sorted by content_hash (caller's responsibility).
    """
    prefix = partition.rstrip("/") + "/" if partition else ""

    with tempfile.TemporaryDirectory() as tmpdir:
        # Stage all photo bytes to the local temp directory.
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

        tmp_output = os.path.join(tmpdir, "output.avif")
        payload = json.dumps({
            "photos": photos_payload,
            "tile_size": tile_size,
            "fit": fit,
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
    photo_entries: list[PhotoEntry]
) -> ThumbnailResult:
    """Generate thumbnail and preview AVIF containers for a partition.

    ``photo_entries`` must have ``.content_hash``, ``.filename``, and
    ``.searchable`` attributes (i.e. ``PhotoEntry`` instances).

    Tile order is deterministic: sorted ascending by ``content_hash``.
    This ensures that a photo's tile index only changes when its content
    changes, not when it is renamed or when unrelated photos are added.
    """
    avif_grid_binary = _find_avif_grid_binary()

    # Sort by content_hash for stable tile indices.
    ordered = sorted(photo_entries, key=lambda e: e.content_hash)

    results: dict[str, tuple[ThumbnailGridLayout, str]] = {}

    for tier, tile_size in TILE_SIZES.items():
        output_path = _avif_path(partition, tier)
        grid, content_hash = await _call_avif_grid(
            backend=backend,
            partition=partition,
            photo_entries=ordered,
            tile_size=tile_size,
            fit=TILE_FIT[tier],
            quality=AVIF_QUALITY[tier],
            output_path=output_path,
            avif_grid_binary=avif_grid_binary,
        )
        results[tier] = (grid, content_hash)

    thumbnail_grid, thumbnails_hash = results["thumbnail"]
    preview_grid, previews_hash = results["preview"]

    return ThumbnailResult(
        thumbnails_hash=thumbnails_hash,
        previews_hash=previews_hash,
        thumbnail_grid=thumbnail_grid,
        preview_grid=preview_grid,
    )
