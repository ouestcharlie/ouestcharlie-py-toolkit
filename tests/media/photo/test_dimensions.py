"""Tests for JPEG XL and RAW display size."""

from pathlib import Path

import pytest

from ouestcharlie_toolkit import Photo
from ouestcharlie_toolkit.media.photo.dimensions import _jxl_display_size, raw_size
from ouestcharlie_toolkit.storage.local import LocalBackend
from tests.format_samples import SAMPLES_DIR

_SAMPLES = SAMPLES_DIR

# Minimal valid JPEG (SOI + JFIF APP0 + EOI) — no EXIF data.
_MINIMAL_JPEG = (
    b"\xff\xd8"  # SOI
    b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"  # APP0
    b"\xff\xd9"  # EOI
)


def _jxl_header(height: int, width: int, orientation: int) -> bytes:
    """Build the start of a bare JPEG XL codestream: SizeHeader + ImageMetadata
    with the orientation field. Nothing after it is valid, but only these fields
    are read."""
    bits: list[int] = []

    def put(value: int, n: int) -> None:
        bits.extend((value >> i) & 1 for i in range(n))

    put(0, 1)  # not small
    put(3, 2)  # 30-bit height
    put(height - 1, 30)
    put(0, 3)  # no ratio
    put(3, 2)
    put(width - 1, 30)
    put(0, 1)  # all_default: no
    put(1, 1)  # extra_fields
    put(orientation - 1, 3)
    data = bytearray((len(bits) + 7) // 8)
    for i, bit in enumerate(bits):
        data[i >> 3] |= bit << (i & 7)
    return b"\xff\x0a" + bytes(data)


@pytest.mark.parametrize(
    ("orientation", "expected"),
    [(1, (300, 200)), (3, (300, 200)), (6, (200, 300)), (8, (200, 300))],
)
def test_jxl_display_size_applies_codestream_orientation(
    tmp_path: Path, orientation: int, expected: tuple[int, int]
) -> None:
    path = tmp_path / "photo.jxl"
    path.write_bytes(_jxl_header(height=200, width=300, orientation=orientation))
    assert _jxl_display_size(path) == expected


@pytest.mark.asyncio
async def test_extract_exif_corrupt_jxl_raises(tmp_path: Path) -> None:
    """A file that is not JPEG XL is an indexing error, not an empty sidecar."""
    (tmp_path / "photo.jxl").write_bytes(b"not a jpeg xl file")
    with pytest.raises(ValueError):
        await Photo(LocalBackend(root=str(tmp_path)), "photo.jxl").extract_exif()


@pytest.mark.asyncio
async def test_extract_exif_truncated_jxl_raises(tmp_path: Path) -> None:
    (tmp_path / "photo.jxl").write_bytes(_jxl_header(200, 300, 1)[:4])
    with pytest.raises(ValueError):
        await Photo(LocalBackend(root=str(tmp_path)), "photo.jxl").extract_exif()


class _FakeRawImage:
    """Stands in for pyexiv2.Image: only the pixel size API is used."""

    def __init__(self, width: int, height: int) -> None:
        self.width, self.height = width, height

    def get_pixel_width(self) -> int:
        return self.width

    def get_pixel_height(self) -> int:
        return self.height


def test_raw_size_prefers_default_crop_size() -> None:
    exif = {"Exif.SubImage1.DefaultCropSize": "3684/1 2758/1"}
    assert raw_size(_FakeRawImage(3700, 2774), exif) == (3684, 2758)


def test_raw_size_ignores_preview_dimension_tags() -> None:
    """NEF IFD0 holds a 160x120 thumbnail: libexiv2's main image size wins."""
    exif = {"Exif.Image.ImageWidth": "160", "Exif.Image.ImageLength": "120"}
    assert raw_size(_FakeRawImage(4548, 3030), exif) == (4548, 3030)


def test_raw_size_unknown_returns_none() -> None:
    assert raw_size(_FakeRawImage(0, 0), {"Exif.Image.DefaultCropSize": "bad"}) is None
