"""Renditions: AVIF thumbnail grids and JPEG previews, built by the image-proc binary.

The only sub-package that imports ouestcharlie_imageproc.
"""

from ouestcharlie_toolkit.renditions.previews import generate_preview_jpeg
from ouestcharlie_toolkit.renditions.thumbnails import (
    GRID_MAX_PHOTOS,
    delete_partition_thumbnails,
    generate_partition_thumbnails,
)

__all__ = [
    "GRID_MAX_PHOTOS",
    "delete_partition_thumbnails",
    "generate_partition_thumbnails",
    "generate_preview_jpeg",
]
