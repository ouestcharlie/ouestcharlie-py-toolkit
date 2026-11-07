"""Tests for XmpStore: sidecar resolution, creation and date-offset recovery."""

import shutil
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from ouestcharlie_toolkit.model.entries import XmpSidecar
from ouestcharlie_toolkit.storage.local import LocalBackend
from ouestcharlie_toolkit.xmp import (
    XmpStore,
    serialize_xmp,
    sidecar_candidates,
)
from tests.format_samples import SAMPLES_DIR

_SAMPLES = SAMPLES_DIR


# ---------------------------------------------------------------------------
# XmpStore — dual-convention read/write resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_prioritizes_full_extension_sidecar():
    """When both conventions exist for a photo, read() prefers the full-extension one."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "photo.jpg").write_bytes(b"data")
        (root / "photo.xmp").write_text(serialize_xmp(XmpSidecar(content_hash="stripped-hash")))
        (root / "photo.jpg.xmp").write_text(serialize_xmp(XmpSidecar(content_hash="full-ext-hash")))
        store = XmpStore(LocalBackend(root=tmpdir))

        sidecar, _ = await store.read("photo.jpg")

    assert sidecar.content_hash == "full-ext-hash"


@pytest.mark.asyncio
async def test_read_falls_back_to_stripped_extension_sidecar():
    """When only the stripped-extension sidecar exists, read() falls back to it."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "photo.jpg").write_bytes(b"data")
        (root / "photo.xmp").write_text(serialize_xmp(XmpSidecar(content_hash="stripped-hash")))
        store = XmpStore(LocalBackend(root=tmpdir))

        sidecar, _ = await store.read("photo.jpg")

    assert sidecar.content_hash == "stripped-hash"


def test_sidecar_candidates_order():
    """Full-extension form first, then the extension-stripped one."""
    assert sidecar_candidates("2024/IMG_001.cr3") == ("2024/IMG_001.cr3.xmp", "2024/IMG_001.xmp")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "present", [("photo.jpg.xmp",), ("photo.xmp",), ("photo.jpg.xmp", "photo.xmp")]
)
async def test_read_resolves_first_existing_candidate(present: tuple[str, ...]):
    """read() opens the first candidate on disk, so a directory listing can
    predict which sidecar (and version token) the reader will use."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "photo.jpg").write_bytes(b"data")
        for name in present:
            (root / name).write_text(serialize_xmp(XmpSidecar(content_hash=name)))
        store = XmpStore(LocalBackend(root=tmpdir))

        sidecar, version = await store.read("photo.jpg")

        expected = next(c for c in sidecar_candidates("photo.jpg") if c in present)
        assert sidecar.content_hash == expected
        assert version.value == (root / expected).stat().st_mtime_ns


@pytest.mark.asyncio
async def test_read_or_create_with_no_sidecar_uses_full_extension_path():
    """A photo with no sidecar at all gets a new one at the full-extension path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        shutil.copy(_SAMPLES / "001.jpg", Path(tmpdir) / "001.jpg")
        store = XmpStore(LocalBackend(root=tmpdir))

        _, _, created = await store.read_or_create_from_picture("001.jpg")

        assert created
        assert (Path(tmpdir) / "001.jpg.xmp").exists()
        assert not (Path(tmpdir) / "001.xmp").exists()


@pytest.mark.asyncio
async def test_modifying_stripped_extension_sidecar_updates_in_place():
    """Modifying a photo whose sidecar exists only in stripped-extension form
    updates that file in place — no full-extension sidecar is created alongside it.

    Uses the versioned tests/sample-images/001.jpg / 001.ref.xmp fixture pair.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        shutil.copy(_SAMPLES / "001.jpg", Path(tmpdir) / "001.jpg")
        shutil.copy(_SAMPLES / "001.ref.xmp", Path(tmpdir) / "001.xmp")
        store = XmpStore(LocalBackend(root=tmpdir))

        def _bump_rating(sidecar: XmpSidecar) -> XmpSidecar:
            sidecar.rating = 5
            return sidecar

        updated = await store.read_modify_write("001.jpg", _bump_rating)

        assert updated.rating == 5
        assert (Path(tmpdir) / "001.xmp").exists()
        assert not (Path(tmpdir) / "001.jpg.xmp").exists()


@pytest.mark.asyncio
async def test_same_stem_different_extensions_get_distinct_sidecars():
    """photo.cr3 and photo.jpg (same stem) get distinct, independently creatable sidecars."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        root.mkdir(exist_ok=True)
        (root / "photo.cr3").write_bytes(b"raw-data")
        (root / "photo.jpg").write_bytes(b"jpeg-data")
        store = XmpStore(LocalBackend(root=tmpdir))

        await store.create("photo.cr3", XmpSidecar(content_hash="cr3-hash"))
        await store.create("photo.jpg", XmpSidecar(content_hash="jpg-hash"))

        cr3_sidecar, _ = await store.read("photo.cr3")
        jpg_sidecar, _ = await store.read("photo.jpg")

        assert cr3_sidecar.content_hash == "cr3-hash"
        assert jpg_sidecar.content_hash == "jpg-hash"
        assert (root / "photo.cr3.xmp").exists()
        assert (root / "photo.jpg.xmp").exists()


def _darktable_xml() -> str:
    return (_SAMPLES / "001.darktable.ref.xmp").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_naive_sidecar_date_recovers_offset_from_photo_exif():
    """001.jpg has EXIF 2026:02:21 13:03:10 with OffsetTimeOriginal=+01:00."""
    from datetime import timedelta, timezone

    with tempfile.TemporaryDirectory() as tmpdir:
        shutil.copy(_SAMPLES / "001.jpg", Path(tmpdir) / "001.jpg")
        (Path(tmpdir) / "001.jpg.xmp").write_text(_darktable_xml(), encoding="utf-8")
        store = XmpStore(LocalBackend(root=tmpdir))

        s, _, created = await store.read_or_create_from_picture("001.jpg")

        assert not created
        assert s.date_taken == datetime(
            2026, 2, 21, 13, 3, 10, 140000, tzinfo=timezone(timedelta(hours=1))
        )
        # Not written back on read.
        assert "2026:02:21 13:03:10.140" in (Path(tmpdir) / "001.jpg.xmp").read_text()


@pytest.mark.asyncio
async def test_naive_sidecar_date_different_from_exif_stays_naive():
    with tempfile.TemporaryDirectory() as tmpdir:
        shutil.copy(_SAMPLES / "001.jpg", Path(tmpdir) / "001.jpg")
        (Path(tmpdir) / "001.jpg.xmp").write_text(
            _darktable_xml().replace("2026:02:21 13:03:10.140", "2026:02:20 09:00:00"),
            encoding="utf-8",
        )
        store = XmpStore(LocalBackend(root=tmpdir))

        s, _, _ = await store.read_or_create_from_picture("001.jpg")

        assert s.date_taken == datetime(2026, 2, 20, 9, 0, 0)
