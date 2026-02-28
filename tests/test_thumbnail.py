"""Tests for the thumbnail decode+resize module."""

import pathlib

import pytest
from PIL import Image

from ouestcharlie_toolkit.thumbnail import decode_and_resize

SAMPLES = pathlib.Path(__file__).parent / "sample-images"

# 001.jpg: 6112×6112 square, orientation 1
# 002.JPG: 2272×1704 landscape, orientation 1


@pytest.mark.parametrize("filename", ["001.jpg", "002.JPG"])
def test_short_edge_is_correct(filename: str) -> None:
    path = str(SAMPLES / filename)
    img = decode_and_resize(path, orientation=1, short_edge=256)
    w, h = img.size
    assert min(w, h) == 256


def test_001_aspect_ratio_preserved() -> None:
    # 001.jpg is 1308×1046 (landscape); short edge is height
    img = decode_and_resize(str(SAMPLES / "001.jpg"), orientation=1, short_edge=256)
    w, h = img.size
    assert h == 256
    assert w > h


def test_landscape_image_aspect_ratio_preserved() -> None:
    # 002.JPG is 2272×1704
    img = decode_and_resize(str(SAMPLES / "002.JPG"), orientation=1, short_edge=256)
    w, h = img.size
    assert h == 256  # short edge
    assert w > h    # still landscape


def test_output_is_rgb() -> None:
    img = decode_and_resize(str(SAMPLES / "001.jpg"), orientation=1, short_edge=64)
    assert img.mode == "RGB"


def test_orientation_none_uses_embedded_exif() -> None:
    # Both samples have orientation=1 embedded; result should be same as explicit orientation=1
    img_explicit = decode_and_resize(str(SAMPLES / "002.JPG"), orientation=1, short_edge=128)
    img_implicit = decode_and_resize(str(SAMPLES / "002.JPG"), orientation=None, short_edge=128)
    assert img_explicit.size == img_implicit.size


def test_rotation_changes_dimensions() -> None:
    # Orientation 6 (rotate 90 CW) should swap width and height for a non-square image.
    img_normal = decode_and_resize(str(SAMPLES / "002.JPG"), orientation=1, short_edge=256)
    img_rotated = decode_and_resize(str(SAMPLES / "002.JPG"), orientation=6, short_edge=256)
    w_n, h_n = img_normal.size
    w_r, h_r = img_rotated.size
    # After 90° rotation, former landscape becomes portrait
    assert w_n > h_n   # original is landscape
    assert h_r > w_r   # rotated should be portrait
    assert min(w_r, h_r) == 256


@pytest.mark.parametrize("short_edge", [64, 128, 256, 512])
def test_various_target_sizes(short_edge: int) -> None:
    img = decode_and_resize(str(SAMPLES / "001.jpg"), orientation=1, short_edge=short_edge)
    assert min(img.size) == short_edge
