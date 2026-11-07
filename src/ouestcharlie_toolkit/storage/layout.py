"""Well-known backend-relative paths under the `.ouestcharlie/` metadata directory."""

SUMMARY_FILENAME = "summary.json"
METADATA_DIR = ".ouestcharlie"
PREVIEW_JPEG_SUBDIR = "previews"
LANCE_INDEX_SUBDIR = "index.lance"


def summary_path() -> str:
    """Well-known path for the root summary file: '.ouestcharlie/summary.json'."""
    return f"{METADATA_DIR}/{SUMMARY_FILENAME}"


def lance_index_path() -> str:
    """Well-known backend-relative path
    for the LanceDB index directory: '.ouestcharlie/index.lance'."""
    return f"{METADATA_DIR}/{LANCE_INDEX_SUBDIR}"


def preview_jpeg_path(partition: str, content_hash: str) -> str:
    """Backend-relative path for a per-photo JPEG preview cache file.

    Example: ``'2024/2024-07'`` → ``'.ouestcharlie/2024/2024-07/previews/Kf3QzA2nBcR8xYvLP9w.jpg'``.
    Root partition (``''``) → ``'.ouestcharlie/previews/Kf3QzA2nBcR8xYvLm1P9w.jpg'``.
    """
    suffix = partition.rstrip("/") + "/" if partition else ""
    return f"{METADATA_DIR}/{suffix}{PREVIEW_JPEG_SUBDIR}/{content_hash}.jpg"


def thumbnail_avif_path(partition: str, avif_hash: str, tier: str = "thumbnail") -> str:
    """Reconstruct the backend-relative path for a thumbnail AVIF chunk.

    Example: thumbnail_avif_path("2024/Jul", "Kf3QzA2_nBcR8xYvLm1P9w")
             → ".ouestcharlie/2024/Jul/thumbnails-Kf3QzA2_nBcR8xYvLm1P9w.avif"
    Root partition (``''``) → ``'.ouestcharlie/thumbnails-{hash}.avif'``.
    """
    suffix = partition.rstrip("/") + "/" if partition else ""
    stem = "thumbnails" if tier == "thumbnail" else "previews"
    return f"{METADATA_DIR}/{suffix}{stem}-{avif_hash}.avif"
