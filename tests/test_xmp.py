"""Test XMP utilities and path helpers."""

from ouestcharlie_toolkit.xmp import xmp_path_for


def test_xmp_path_for_simple():
    """Test XMP path generation for simple filename."""
    xmp_path = xmp_path_for("photo.jpg")
    assert xmp_path == "photo.xmp"


def test_xmp_path_for_with_directory():
    """Test XMP path generation with directory path."""
    xmp_path = xmp_path_for("2024/IMG_001.jpg")
    assert xmp_path == "2024/IMG_001.xmp"


def test_xmp_path_for_nested():
    """Test XMP path generation for nested directories."""
    xmp_path = xmp_path_for("2024/2024-07/vacation/IMG_001.jpg")
    assert xmp_path == "2024/2024-07/vacation/IMG_001.xmp"


def test_xmp_path_for_different_extensions():
    """Test XMP path generation for various file extensions."""
    assert xmp_path_for("photo.JPG") == "photo.xmp"
    assert xmp_path_for("photo.dng") == "photo.xmp"
    assert xmp_path_for("photo.cr2") == "photo.xmp"
    assert xmp_path_for("photo.nef") == "photo.xmp"


def test_xmp_path_for_no_extension():
    """Test XMP path generation for files without extension."""
    xmp_path = xmp_path_for("photo")
    assert xmp_path == "photo.xmp"


def test_xmp_path_for_multiple_dots():
    """Test XMP path generation for filenames with multiple dots."""
    xmp_path = xmp_path_for("my.photo.backup.jpg")
    assert xmp_path == "my.photo.backup.xmp"
