"""Thumbnail resizing utilities for OuEstCharlie."""

from __future__ import annotations

import os

from PIL import Image, ImageOps

# TIFF orientation value → Pillow transpose operation to correct it.
# Orientation 1 maps to None (already upright, nothing to do).
_ORIENTATION_TRANSPOSE: dict[int, Image.Transpose] = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}

_RAW_EXTENSIONS = {".cr2", ".cr3", ".nef", ".arw", ".dng", ".raf", ".orf", ".rw2", ".pef"}
_HEIC_EXTENSIONS = {".heic", ".heif"}


def decode_and_resize(path: str, orientation: int | None, short_edge: int) -> Image.Image:
    """Decode a photo file and resize so its short edge equals ``short_edge`` pixels.

    Orientation is applied using the TIFF orientation value (1–8) from the
    ``orientation`` parameter (sourced from the XMP sidecar / manifest).  When
    ``orientation`` is ``None`` the orientation embedded in the file is used via
    Pillow's ``exif_transpose``.

    Supported formats:
    - JPEG, PNG, AVIF — via Pillow
    - HEIC/HEIF       — via Pillow + pillow-heif (``pip install ouestcharlie-toolkit[heic]``)
    - RAW (CR2, NEF, ARW, DNG, …) — via rawpy (``pip install ouestcharlie-toolkit[raw]``)

    Returns an RGB PIL Image.
    """
    ext = os.path.splitext(path)[1].lower()

    if ext in _RAW_EXTENSIONS:
        img = _open_raw(path)
    elif ext in _HEIC_EXTENSIONS:
        img = _open_heic(path)
    else:
        img = Image.open(path)
        img.load()

    img = img.convert("RGB")

    # Apply orientation correction.
    if orientation is not None:
        transpose_op = _ORIENTATION_TRANSPOSE.get(orientation)
        if transpose_op is not None:
            img = img.transpose(transpose_op)
    else:
        img = ImageOps.exif_transpose(img)

    # Resize preserving aspect ratio so short edge == short_edge.
    w, h = img.size
    if w <= h:
        new_w = short_edge
        new_h = round(h * short_edge / w)
    else:
        new_h = short_edge
        new_w = round(w * short_edge / h)
    return img.resize((new_w, new_h), Image.Resampling.LANCZOS)


def _open_raw(path: str) -> Image.Image:
    try:
        import rawpy  # type: ignore[import-untyped]
    except ImportError as exc:
        raise ImportError(
            "rawpy is required for RAW files. "
            "Install with: pip install 'ouestcharlie-toolkit[raw]'"
        ) from exc
    with rawpy.imread(path) as raw:
        rgb = raw.postprocess(use_camera_wb=True, output_bps=8)
    return Image.fromarray(rgb)


def _open_heic(path: str) -> Image.Image:
    try:
        import pillow_heif  # type: ignore[import-untyped]
        pillow_heif.register_heif_opener()
    except ImportError as exc:
        raise ImportError(
            "pillow-heif is required for HEIC/HEIF files. "
            "Install with: pip install 'ouestcharlie-toolkit[heic]'"
        ) from exc
    img = Image.open(path)
    img.load()
    return img
