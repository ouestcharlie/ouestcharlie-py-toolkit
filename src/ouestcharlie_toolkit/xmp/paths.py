"""Sidecar file names: the two naming conventions and the order they are tried in."""

from __future__ import annotations

from pathlib import Path


def xmp_path_for(photo_path: str, *, with_photo_extension: bool) -> str:
    """Compute the XMP sidecar path for a photo file.

    Two conventions are in use across the photography ecosystem:
    ``with_photo_extension=True`` keeps the photo's extension in the sidecar
    name (Darktable, digiKam, Immich's preferred form — e.g.
    ``IMG_001.cr3.xmp``); ``with_photo_extension=False`` strips it
    (Lightroom's convention — e.g. ``IMG_001.xmp``).

    Args:
        photo_path: Path to the photo file (e.g., "2024/2024-07/IMG_001.jpg").
        with_photo_extension: Whether to keep the photo's extension in the
            sidecar filename.

    Returns:
        Path to the XMP sidecar.
    """
    p = Path(photo_path)
    if with_photo_extension:
        return (p.parent / (p.name + ".xmp")).as_posix()
    return p.with_suffix(".xmp").as_posix()


def sidecar_candidates(photo_path: str) -> tuple[str, str]:
    """Sidecar paths of a media file, in the order the reader tries them.

    The full-extension form (``IMG_001.cr3.xmp``) comes first, then the
    extension-stripped form (``IMG_001.xmp``). Code that predicts which
    sidecar ``XmpStore.read`` will pick (e.g. from a directory listing) must
    use this order.
    """
    return (
        xmp_path_for(photo_path, with_photo_extension=True),
        xmp_path_for(photo_path, with_photo_extension=False),
    )
