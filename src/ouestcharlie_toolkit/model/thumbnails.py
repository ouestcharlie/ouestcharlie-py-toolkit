"""Layout of the AVIF thumbnail and preview grids."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ThumbnailGridLayout:
    """Grid layout metadata for a thumbnail or preview AVIF container.

    Tiles are ordered by photo content_hash (ascending) for stability:
    a photo's tile index only changes if its content changes, not on renames.

    Column count is always 8 — not stored.
    """

    rows: int  # number of rows in the AVIF grid
    tile_size: int  # short edge in pixels (e.g. 256 or 1440)
    photo_order: list[str]  # content_hashes in row-major tile order


@dataclass
class ThumbnailChunk:
    """One AVIF grid file for a subset of photos in a partition.

    A partition's thumbnails are split into chunks of at most GRID_MAX_PHOTOS
    (64) photos each, producing a max 8×8 grid per file.  Each chunk is
    identified by its content hash, which is used as part of its filename
    (``thumbnails-{avif_hash}.avif``).

    The backend path is not stored — reconstruct it with
    ``thumbnail_avif_path(partition, chunk.avif_hash)``.
    """

    avif_hash: str  # 22-char BLAKE3 of the AVIF content
    grid: ThumbnailGridLayout  # rows, tile_size, photo_order
