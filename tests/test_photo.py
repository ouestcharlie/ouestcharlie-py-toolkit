"""Tests for the Photo domain class."""

import hashlib
import logging
import tempfile
from pathlib import Path

import pytest

from ouestcharlie_toolkit import Photo
from ouestcharlie_toolkit.backends.local import LocalBackend
from ouestcharlie_toolkit.photo import (
    _decode_undefined_ascii,
    _parse_exif_datetime,
    _parse_exif_gps,
)
from ouestcharlie_toolkit.xmp import _parse_iso_datetime, parse_xmp

_SAMPLES = Path(__file__).parent / "sample-images"
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


# ---------------------------------------------------------------------------
# Real-image EXIF extraction vs reference XMP
# ---------------------------------------------------------------------------

_XMP_CREATE_DATE = "{http://ns.adobe.com/xap/1.0/}CreateDate"


def _sample_pairs():
    """Yield pytest.param(image_path, ref_xmp_path) for each *.ref.xmp with a matching image."""
    pairs = []
    for ref in sorted(_SAMPLES.glob("*.ref.xmp")):
        stem = ref.name[: -len(".ref.xmp")]
        candidates = [
            p for p in _SAMPLES.glob(f"{stem}.*")
            if p.suffix.lower() not in (".xmp",) and p != ref
        ]
        if candidates:
            pairs.append(pytest.param(candidates[0], ref, id=stem))
    return pairs


@pytest.mark.asyncio
@pytest.mark.parametrize("image_path,ref_xmp_path", _sample_pairs())
async def test_extract_exif_matches_ref(image_path, ref_xmp_path):
    """Extract EXIF from a real image and compare typed fields to the reference XMP.

    *.ref.xmp files are produced by Exiv2 and represent the ground truth for
    what metadata is in each image.  Our parser reads make/model/orientation
    from tiff:* attributes.  The date is stored as xmp:CreateDate (not
    exif:DateTimeOriginal) in the ref, so it lands in _extra; we parse it from
    there to keep the expected value fully ref-driven.

    The wall-clock portion of date_taken is compared against ref_date.  Some
    images carry OffsetTimeOriginal so extract_exif() will attach a timezone;
    others will not — we do not assert tzinfo presence here.
    """
    ref_sidecar = parse_xmp(ref_xmp_path.read_text(encoding="utf-8"))
    ref_date = _parse_iso_datetime(ref_sidecar._extra.get(_XMP_CREATE_DATE))

    sidecar = await Photo(
        LocalBackend(root=str(_SAMPLES)), image_path.name
    ).extract_exif()

    assert sidecar.content_hash is not None
    assert sidecar.content_hash.startswith("sha256:")
    assert sidecar.camera_make == ref_sidecar.camera_make
    assert sidecar.camera_model == ref_sidecar.camera_model
    assert sidecar.orientation == ref_sidecar.orientation
    assert sidecar.gps == ref_sidecar.gps

    if ref_date is not None:
        assert sidecar.date_taken is not None
        assert sidecar.date_taken.replace(tzinfo=None) == ref_date

    # Cross-check _extra: compare simple scalar attributes present in both.
    # Skip ref entries whose value begins with "<" — those are complex XMP
    # child elements (rdf:Seq, structs) that Exiv2 serializes differently from
    # raw EXIF strings.  Also skip keys absent from sidecar._extra (Exiv2 may
    # emit XMP-only attributes that have no direct EXIF counterpart).
    mismatches = {}
    for key, ref_val in ref_sidecar._extra.items():
        if ref_val.startswith("<"):
            continue  # complex element — format differs
        our_val = sidecar._extra.get(key)
        if our_val is None:
            continue  # key only in ref (XMP-only field)
        if our_val != ref_val:
            mismatches[key] = (our_val, ref_val)
    assert mismatches == {}, f"_extra value mismatches: {mismatches}"


# ---------------------------------------------------------------------------
# Logging behaviour in EXIF helpers
# ---------------------------------------------------------------------------

_PHOTO_LOGGER = "ouestcharlie_toolkit.photo"


def test_parse_exif_datetime_invalid_logs_debug(caplog):
    """_parse_exif_datetime with an unparseable string emits a DEBUG message."""
    bad_exif = {"Exif.Photo.DateTimeOriginal": "not a date"}
    with caplog.at_level(logging.DEBUG, logger=_PHOTO_LOGGER):
        result = _parse_exif_datetime(bad_exif)
    assert result is None
    assert any("Could not parse EXIF datetime" in msg for msg in caplog.messages)
    assert any(r.levelno == logging.DEBUG for r in caplog.records)


def test_decode_undefined_ascii_invalid_logs_debug(caplog):
    """_decode_undefined_ascii with non-integer bytes emits a DEBUG message."""
    with caplog.at_level(logging.DEBUG, logger=_PHOTO_LOGGER):
        result = _decode_undefined_ascii("xx yy zz")  # spaces → triggers decode path, then fails
    assert result == "xx yy zz"  # original value returned on failure
    assert any("Could not decode UNDEFINED ASCII" in msg for msg in caplog.messages)


def test_parse_exif_gps_invalid_logs_debug(caplog):
    """_parse_exif_gps with malformed rational strings emits a DEBUG message."""
    bad_exif = {
        "Exif.GPSInfo.GPSLatitudeRef": "N",
        "Exif.GPSInfo.GPSLongitudeRef": "E",
        "Exif.GPSInfo.GPSLatitude": "not/valid",
        "Exif.GPSInfo.GPSLongitude": "2/1",
    }
    with caplog.at_level(logging.DEBUG, logger=_PHOTO_LOGGER):
        result = _parse_exif_gps(bad_exif)
    assert result is None
    assert any("Could not parse EXIF GPS" in msg for msg in caplog.messages)
    assert any(r.levelno == logging.DEBUG for r in caplog.records)
