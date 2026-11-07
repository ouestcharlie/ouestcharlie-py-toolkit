"""Display size of formats whose EXIF dimensions are unreliable: AVIF, JPEG XL, RAW."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from ouestcharlie_toolkit.media.photo.exif import exif_rational_to_float

# ---------------------------------------------------------------------------
# AVIF and JPEG XL: display size and orientation
# ---------------------------------------------------------------------------

# pyexiv2 reads the EXIF of both formats, but their decoders in image-proc
# (libheif, jxl-oxide) apply the rotation stored in the file itself (AVIF
# irot/imir boxes, JXL codestream header), not the EXIF orientation. These
# helpers read that rotation so the sidecar follows the upright convention, like
# HEIC: orientation 1 and display width/height.


def _avif_display_size(path: Path) -> tuple[int, int]:
    """Return the display (width, height) of an AVIF file.

    Pillow returns the stored pixels and reports the irot/imir transform as the
    EXIF orientation (it overrides any orientation tag in the EXIF block).
    """
    from PIL import Image

    with Image.open(path) as img:
        width, height = img.size
        if img.getexif().get(274) in (5, 6, 7, 8):
            width, height = height, width
    return width, height


_JXL_CODESTREAM_SIGNATURE = b"\xff\x0a"
_JXL_CONTAINER_SIGNATURE = b"\x00\x00\x00\x0cJXL \r\n\x87\n"
# SizeHeader aspect ratios (width / height), indexed by the 3-bit ratio field.
_JXL_RATIOS: dict[int, tuple[int, int]] = {
    1: (1, 1),
    2: (12, 10),
    3: (4, 3),
    4: (3, 2),
    5: (16, 9),
    6: (5, 4),
    7: (2, 1),
}


def _jxl_codestream_start(path: Path, length: int = 32) -> bytes:
    """Return the first bytes of the JPEG XL codestream, bare or inside a container."""
    with path.open("rb") as f:
        head = f.read(len(_JXL_CONTAINER_SIGNATURE))
        if head.startswith(_JXL_CODESTREAM_SIGNATURE):
            return head + f.read(length)
        if head != _JXL_CONTAINER_SIGNATURE:
            raise ValueError("not a JPEG XL file")
        # ISO BMFF boxes: walk to the codestream box (jxlc, or the first jxlp part).
        while len(box := f.read(8)) == 8:
            size, box_type = int.from_bytes(box[:4], "big"), box[4:]
            header = 8
            if size == 1:
                size, header = int.from_bytes(f.read(8), "big"), 16
            if box_type == b"jxlc":
                return f.read(length)
            if box_type == b"jxlp":
                f.read(4)  # part index
                return f.read(length)
            if size < header:
                break
            f.seek(size - header, 1)
    raise ValueError("JPEG XL container has no codestream")


def _jxl_display_size(path: Path) -> tuple[int, int]:
    """Return the display (width, height) of a JPEG XL file, from its codestream header.

    Pillow cannot open JPEG XL, so the SizeHeader and the orientation field of
    ImageMetadata are parsed here (JPEG XL spec, ISO/IEC 18181-1, section A).
    """
    stream = _jxl_codestream_start(path)
    if not stream.startswith(_JXL_CODESTREAM_SIGNATURE):
        raise ValueError("invalid JPEG XL codestream")
    data = stream[len(_JXL_CODESTREAM_SIGNATURE) :]
    pos = 0

    def bits(n: int) -> int:
        nonlocal pos
        value = 0
        for i in range(n):
            if pos >> 3 >= len(data):
                raise ValueError("truncated JPEG XL header")
            value |= ((data[pos >> 3] >> (pos & 7)) & 1) << i
            pos += 1
        return value

    def dimension(small: int) -> int:
        if small:
            return (bits(5) + 1) * 8
        return bits((9, 13, 18, 30)[bits(2)]) + 1

    small = bits(1)
    height = dimension(small)
    ratio = bits(3)
    if ratio:
        num, den = _JXL_RATIOS[ratio]
        width = height * num // den
    else:
        width = dimension(small)

    orientation = 1
    all_default = bits(1)
    if not all_default and bits(1):  # extra_fields
        orientation = bits(3) + 1
    if orientation in (5, 6, 7, 8):
        width, height = height, width
    return width, height


# ---------------------------------------------------------------------------
# RAW: size of the main image
# ---------------------------------------------------------------------------


def raw_size(img: Any, exif: dict[str, str]) -> tuple[int, int] | None:
    """Return the (width, height) of a RAW file's main image, as stored.

    The plain EXIF dimension tags of a RAW file often describe an embedded
    preview (a 160x120 thumbnail in NEF IFD0, a 1920 px JPEG in RAF). libexiv2
    knows where each format keeps the main image (``get_pixel_width``), which
    may include a few sensor border pixels; a DNG ``DefaultCropSize`` gives the
    exact size when present. CR3 is a known gap: libexiv2 reports the preview.
    """
    for key, value in exif.items():
        if key.endswith(".DefaultCropSize"):
            try:
                width, height = (round(exif_rational_to_float(v)) for v in value.split())
            except ValueError, ZeroDivisionError:
                continue
            if width > 0 and height > 0:
                return width, height
    width, height = img.get_pixel_width(), img.get_pixel_height()
    return (width, height) if width > 0 and height > 0 else None


# EXIF read by pyexiv2, display size and orientation read from the file itself.
UPRIGHT_DISPLAY_SIZE: dict[str, Callable[[Path], tuple[int, int]]] = {
    ".avif": _avif_display_size,
    ".jxl": _jxl_display_size,
}
