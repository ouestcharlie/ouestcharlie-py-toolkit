"""Tests for sidecar file naming."""

from ouestcharlie_toolkit.xmp import (
    xmp_path_for,
)
from tests.format_samples import SAMPLES_DIR

_SAMPLES = SAMPLES_DIR


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def test_xmp_path_for_simple():
    """Test XMP path generation for simple filename, both conventions."""
    assert xmp_path_for("photo.jpg", with_photo_extension=True) == "photo.jpg.xmp"
    assert xmp_path_for("photo.jpg", with_photo_extension=False) == "photo.xmp"


def test_xmp_path_for_with_directory():
    """Test XMP path generation with directory path, both conventions."""
    assert xmp_path_for("2024/IMG_001.jpg", with_photo_extension=True) == "2024/IMG_001.jpg.xmp"
    assert xmp_path_for("2024/IMG_001.jpg", with_photo_extension=False) == "2024/IMG_001.xmp"


def test_xmp_path_for_nested():
    """Test XMP path generation for nested directories, both conventions."""
    assert (
        xmp_path_for("2024/2024-07/vacation/IMG_001.jpg", with_photo_extension=True)
        == "2024/2024-07/vacation/IMG_001.jpg.xmp"
    )
    assert (
        xmp_path_for("2024/2024-07/vacation/IMG_001.jpg", with_photo_extension=False)
        == "2024/2024-07/vacation/IMG_001.xmp"
    )


def test_xmp_path_for_different_extensions_no_collision():
    """With with_photo_extension=True, different extensions must not collide."""
    assert xmp_path_for("photo.JPG", with_photo_extension=True) == "photo.JPG.xmp"
    assert xmp_path_for("photo.dng", with_photo_extension=True) == "photo.dng.xmp"
    assert xmp_path_for("photo.cr2", with_photo_extension=True) == "photo.cr2.xmp"
    assert xmp_path_for("photo.nef", with_photo_extension=True) == "photo.nef.xmp"


def test_xmp_path_for_different_extensions_stripped_collide():
    """With with_photo_extension=False, different extensions collapse to the same sidecar."""
    assert xmp_path_for("photo.JPG", with_photo_extension=False) == "photo.xmp"
    assert xmp_path_for("photo.dng", with_photo_extension=False) == "photo.xmp"
    assert xmp_path_for("photo.cr2", with_photo_extension=False) == "photo.xmp"
    assert xmp_path_for("photo.nef", with_photo_extension=False) == "photo.xmp"


def test_xmp_path_for_no_extension():
    """Test XMP path generation for files without extension, both conventions."""
    assert xmp_path_for("photo", with_photo_extension=True) == "photo.xmp"
    assert xmp_path_for("photo", with_photo_extension=False) == "photo.xmp"


def test_xmp_path_for_multiple_dots():
    """Test XMP path generation for filenames with multiple dots, both conventions."""
    assert (
        xmp_path_for("my.photo.backup.jpg", with_photo_extension=True) == "my.photo.backup.jpg.xmp"
    )
    assert xmp_path_for("my.photo.backup.jpg", with_photo_extension=False) == "my.photo.backup.xmp"
