"""Tests for the Photo domain class."""

import hashlib
import tempfile
from pathlib import Path

import pytest

from ouestcharlie_toolkit import Photo
from ouestcharlie_toolkit.backends.local import LocalBackend
from ouestcharlie_toolkit.schema import XmpSidecar

# Minimal valid JPEG (SOI + JFIF APP0 + EOI) — no EXIF data.
_MINIMAL_JPEG = (
    b"\xff\xd8"  # SOI
    b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"  # APP0
    b"\xff\xd9"  # EOI
)


# ---------------------------------------------------------------------------
# create_identity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_identity_returns_sha256_prefix():
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "photo.jpg").write_bytes(b"data")
        photo = Photo(LocalBackend(root=tmpdir), "photo.jpg")
        identity = await photo.create_identity()
    assert identity.startswith("sha256:")


@pytest.mark.asyncio
async def test_create_identity_correct_hash():
    data = b"Hello, OuEstCharlie!"
    expected = "sha256:" + hashlib.sha256(data).hexdigest()
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "photo.jpg").write_bytes(data)
        identity = await Photo(LocalBackend(root=tmpdir), "photo.jpg").create_identity()
    assert identity == expected


@pytest.mark.asyncio
async def test_create_identity_cached():
    """Second call returns cached hash without re-reading the file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "photo.jpg").write_bytes(b"data")
        backend = LocalBackend(root=tmpdir)
        photo = Photo(backend, "photo.jpg")

        hash1 = await photo.create_identity()
        hash2 = await photo.create_identity()

    assert hash1 == hash2
    assert photo._content_hash == hash1


@pytest.mark.asyncio
async def test_create_identity_empty_file():
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "empty.jpg").write_bytes(b"")
        identity = await Photo(LocalBackend(root=tmpdir), "empty.jpg").create_identity()
    assert identity == "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


# ---------------------------------------------------------------------------
# extract_exif
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_exif_returns_sidecar():
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "photo.jpg").write_bytes(_MINIMAL_JPEG)
        sidecar = await Photo(LocalBackend(root=tmpdir), "photo.jpg").extract_exif()
    assert isinstance(sidecar, XmpSidecar)


@pytest.mark.asyncio
async def test_extract_exif_sets_content_hash():
    expected = "sha256:" + hashlib.sha256(_MINIMAL_JPEG).hexdigest()
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "photo.jpg").write_bytes(_MINIMAL_JPEG)
        sidecar = await Photo(LocalBackend(root=tmpdir), "photo.jpg").extract_exif()
    assert sidecar.content_hash == expected


@pytest.mark.asyncio
async def test_extract_exif_no_exif_fields_are_none():
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "photo.jpg").write_bytes(_MINIMAL_JPEG)
        sidecar = await Photo(LocalBackend(root=tmpdir), "photo.jpg").extract_exif()
    assert sidecar.date_taken is None
    assert sidecar.camera_make is None
    assert sidecar.camera_model is None
    assert sidecar.gps is None


# ---------------------------------------------------------------------------
# Interaction between the two methods
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_exif_caches_hash_for_create_identity():
    """extract_exif() caches the hash so create_identity() is free."""
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "photo.jpg").write_bytes(_MINIMAL_JPEG)
        backend = LocalBackend(root=tmpdir)
        photo = Photo(backend, "photo.jpg")

        sidecar = await photo.extract_exif()
        # Hash must be cached at this point
        assert photo._content_hash is not None
        identity = await photo.create_identity()

    assert identity == sidecar.content_hash
