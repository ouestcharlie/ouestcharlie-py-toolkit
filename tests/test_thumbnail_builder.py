"""Tests for thumbnail_builder — AVIF grid generation pipeline."""

from __future__ import annotations

from ouestcharlie_toolkit.hashing import content_hash as _content_hash
import json
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouestcharlie_toolkit.backends.local import LocalBackend
from ouestcharlie_toolkit.schema import METADATA_DIR, ThumbnailGridLayout
from ouestcharlie_toolkit.thumbnail_builder import (
    _avif_path,
    _call_image_proc,
    _find_image_proc_binary,
    _stage_photos,
    generate_partition_thumbnails,
)

_SAMPLE_JPG = Path(__file__).parent / "sample-images" / "001.jpg"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_photo_entry(filename: str, content_hash: str, orientation: int | None = 1):
    """Return a minimal object with the attributes PhotoEntry needs."""
    entry = MagicMock()
    entry.filename = filename
    entry.content_hash = content_hash
    entry.searchable = {"orientation": orientation}
    return entry


class _FakeAvifProcess:
    """Fake asyncio subprocess that writes a placeholder AVIF and returns grid JSON."""

    def __init__(self, cols: int = 2, rows: int = 1, tile_size: int = 256) -> None:
        self.returncode = 0
        self._cols = cols
        self._rows = rows
        self._tile_size = tile_size

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        photo_order: list[str] = []
        if input:
            data = json.loads(input.decode())
            Path(data["output"]).write_bytes(b"FAKE_AVIF_CONTENT")
            photo_order = [p["content_hash"] for p in data.get("photos", [])]
        stdout = json.dumps({
            "cols": self._cols,
            "rows": self._rows,
            "tileSize": self._tile_size,
            "photoOrder": photo_order,
        }).encode()
        return stdout, b""


class _FakeAvifProcessError:
    """Fake asyncio subprocess that simulates avif-grid failure."""

    returncode = 1

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        return b"", b"encoding failed: bad photo"


# ---------------------------------------------------------------------------
# _avif_path
# ---------------------------------------------------------------------------


def test_avif_path_thumbnail_tier() -> None:
    assert _avif_path("2024/July", "thumbnail") == f"2024/July/{METADATA_DIR}/thumbnails.avif"


def test_avif_path_preview_tier() -> None:
    assert _avif_path("2024/July", "preview") == f"2024/July/{METADATA_DIR}/previews.avif"


def test_avif_path_root_partition() -> None:
    assert _avif_path("", "thumbnail") == f"{METADATA_DIR}/thumbnails.avif"


# ---------------------------------------------------------------------------
# _find_avif_grid_binary
# ---------------------------------------------------------------------------


def test_find_binary_uses_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AVIF_GRID_BINARY", "/custom/avif-grid")
    assert _find_image_proc_binary() == "/custom/avif-grid"


def test_find_binary_uses_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AVIF_GRID_BINARY", raising=False)
    monkeypatch.delenv("IMAGE_PROC_BINARY", raising=False)
    with patch("shutil.which", return_value="/usr/local/bin/image-proc"):
        assert _find_image_proc_binary() == "/usr/local/bin/image-proc"


def test_find_binary_raises_when_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AVIF_GRID_BINARY", raising=False)
    monkeypatch.delenv("IMAGE_PROC_BINARY", raising=False)
    with patch("shutil.which", return_value=None):
        with patch("pathlib.Path.exists", return_value=False):
            with pytest.raises(FileNotFoundError, match="image-proc binary not found"):
                _find_image_proc_binary()


# ---------------------------------------------------------------------------
# _call_avif_grid
# ---------------------------------------------------------------------------


@pytest.fixture()
def backend_with_photo(tmp_path: Path) -> LocalBackend:
    shutil.copy(_SAMPLE_JPG, tmp_path / "photo.jpg")
    return LocalBackend(root=str(tmp_path))


def _staged(tmp_path: Path, content_hash: str, ext: str = ".jpg", orientation: int | None = 1) -> dict:
    """Build a pre-staged photo dict (as _stage_photos would produce)."""
    p = tmp_path / f"photo{ext}"
    p.write_bytes(b"FAKE_PHOTO")
    return {"path": str(p), "ext": ext, "orientation": orientation, "content_hash": content_hash}


@pytest.mark.asyncio
async def test_call_avif_grid_writes_avif_to_backend(
    backend_with_photo: LocalBackend, tmp_path: Path
) -> None:
    staged = [_staged(tmp_path, "sha256:" + "aa" * 32)]
    output_path = f"{METADATA_DIR}/thumbnails.avif"

    with patch(
        "asyncio.create_subprocess_exec",
        return_value=_FakeAvifProcess(cols=1, rows=1, tile_size=256),
    ):
        grid, content_hash = await _call_image_proc(
            backend=backend_with_photo,
            staged_photos=staged,
            tile_size=256,
            fit="crop",
            quality=55,
            output_path=output_path,
            tmpdir=str(tmp_path),
            binary="fake-avif-grid",
        )

    assert await backend_with_photo.exists(output_path)
    assert grid.cols == 1
    assert grid.rows == 1
    assert grid.tile_size == 256


@pytest.mark.asyncio
async def test_call_avif_grid_returns_content_hash(
    backend_with_photo: LocalBackend, tmp_path: Path
) -> None:
    staged = [_staged(tmp_path, "sha256:" + "bb" * 32)]

    with patch(
        "asyncio.create_subprocess_exec",
        return_value=_FakeAvifProcess(),
    ):
        _, content_hash = await _call_image_proc(
            backend=backend_with_photo,
            staged_photos=staged,
            tile_size=256,
            fit="crop",
            quality=55,
            output_path=f"{METADATA_DIR}/thumbnails.avif",
            tmpdir=str(tmp_path),
            binary="fake-avif-grid",
        )

    assert content_hash == _content_hash(b"FAKE_AVIF_CONTENT")


@pytest.mark.asyncio
async def test_call_avif_grid_passes_correct_json(
    backend_with_photo: LocalBackend, tmp_path: Path
) -> None:
    """The JSON payload sent to avif-grid must include photos, tile_size, fit, quality."""
    staged = [_staged(tmp_path, "sha256:" + "cc" * 32, orientation=6)]
    captured: list[dict] = []

    class _CapturingProcess:
        returncode = 0

        async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
            if input:
                data = json.loads(input.decode())
                captured.append(data)
                Path(data["output"]).write_bytes(b"X")
            return json.dumps({
                "cols": 1, "rows": 1, "tileSize": 256,
                "photoOrder": ["sha256:" + "cc" * 32],
            }).encode(), b""

    with patch("asyncio.create_subprocess_exec", return_value=_CapturingProcess()):
        await _call_image_proc(
            backend=backend_with_photo,
            staged_photos=staged,
            tile_size=256,
            fit="crop",
            quality=55,
            output_path=f"{METADATA_DIR}/thumbnails.avif",
            tmpdir=str(tmp_path),
            binary="fake-avif-grid",
        )

    assert len(captured) == 1
    payload = captured[0]
    assert payload["tile_size"] == 256
    assert payload["fit"] == "crop"
    assert payload["quality"] == 55
    assert len(payload["photos"]) == 1
    assert payload["photos"][0]["ext"] == ".jpg"
    assert payload["photos"][0]["orientation"] == 6
    assert payload["photos"][0]["content_hash"] == "sha256:" + "cc" * 32


@pytest.mark.asyncio
async def test_call_avif_grid_raises_on_nonzero_exit(
    backend_with_photo: LocalBackend, tmp_path: Path
) -> None:
    staged = [_staged(tmp_path, "sha256:" + "dd" * 32)]

    with patch(
        "asyncio.create_subprocess_exec",
        return_value=_FakeAvifProcessError(),
    ):
        with pytest.raises(RuntimeError, match="image-proc exited 1"):
            await _call_image_proc(
                backend=backend_with_photo,
                staged_photos=staged,
                tile_size=256,
                fit="crop",
                quality=55,
                output_path=f"{METADATA_DIR}/thumbnails.avif",
                tmpdir=str(tmp_path),
                binary="fake-avif-grid",
            )


@pytest.mark.asyncio
async def test_call_avif_grid_overwrites_existing_output(
    backend_with_photo: LocalBackend, tmp_path: Path
) -> None:
    staged = [_staged(tmp_path, "sha256:" + "ee" * 32)]
    output_path = f"{METADATA_DIR}/thumbnails.avif"

    # Pre-create the output file.
    out_abs = tmp_path / METADATA_DIR / "thumbnails.avif"
    out_abs.parent.mkdir(parents=True, exist_ok=True)
    out_abs.write_bytes(b"OLD_CONTENT")

    with patch(
        "asyncio.create_subprocess_exec",
        return_value=_FakeAvifProcess(),
    ):
        _, content_hash = await _call_image_proc(
            backend=backend_with_photo,
            staged_photos=staged,
            tile_size=256,
            fit="crop",
            quality=55,
            output_path=output_path,
            tmpdir=str(tmp_path),
            binary="fake-avif-grid",
        )

    assert out_abs.read_bytes() == b"FAKE_AVIF_CONTENT"
    assert content_hash == _content_hash(b"FAKE_AVIF_CONTENT")


@pytest.mark.asyncio
async def test_call_avif_grid_photo_order_in_grid(
    backend_with_photo: LocalBackend, tmp_path: Path
) -> None:
    """photo_order in the returned grid must reflect the photoOrder from Rust output."""
    hashes = ["sha256:" + "aa" * 32, "sha256:" + "bb" * 32]
    staged = [_staged(tmp_path, h) for h in hashes]

    with patch(
        "asyncio.create_subprocess_exec",
        return_value=_FakeAvifProcess(cols=2, rows=1, tile_size=256),
    ):
        grid, _ = await _call_image_proc(
            backend=backend_with_photo,
            staged_photos=staged,
            tile_size=256,
            fit="crop",
            quality=55,
            output_path=f"{METADATA_DIR}/thumbnails.avif",
            tmpdir=str(tmp_path),
            binary="fake-avif-grid",
        )

    assert grid.photo_order == hashes


# ---------------------------------------------------------------------------
# generate_partition_thumbnails
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_partition_thumbnails_returns_result(tmp_path: Path) -> None:
    backend = LocalBackend(root=str(tmp_path))
    photos = [
        _fake_photo_entry("b.jpg", "sha256:" + "bb" * 32),
        _fake_photo_entry("a.jpg", "sha256:" + "aa" * 32),
    ]

    fake_grid = ThumbnailGridLayout(cols=2, rows=1, tile_size=256, photo_order=[])

    with (
        patch("ouestcharlie_toolkit.thumbnail_builder._find_image_proc_binary", return_value="fake-bin"),
        patch("ouestcharlie_toolkit.thumbnail_builder._stage_photos", new=AsyncMock(return_value=[])),
        patch("ouestcharlie_toolkit.thumbnail_builder._call_image_proc", new=AsyncMock(
            return_value=(fake_grid, "A" * 22)
        )),
    ):
        grid, content_hash = await generate_partition_thumbnails(backend, "", photos)

    assert grid is fake_grid
    assert len(content_hash) == 22


@pytest.mark.asyncio
async def test_generate_partition_thumbnails_tiles_sorted_by_hash(tmp_path: Path) -> None:
    """Photos passed to _stage_photos must be sorted by content_hash."""
    backend = LocalBackend(root=str(tmp_path))
    photos = [
        _fake_photo_entry("z.jpg", "sha256:" + "zz" * 32),
        _fake_photo_entry("a.jpg", "sha256:" + "aa" * 32),
        _fake_photo_entry("m.jpg", "sha256:" + "mm" * 32),
    ]

    captured_entries: list[list] = []

    async def capture_stage(backend, partition, photo_entries, tmpdir):
        captured_entries.append(list(photo_entries))
        return []

    fake_grid = ThumbnailGridLayout(cols=2, rows=2, tile_size=256, photo_order=[])

    with (
        patch("ouestcharlie_toolkit.thumbnail_builder._find_image_proc_binary", return_value="fake-bin"),
        patch("ouestcharlie_toolkit.thumbnail_builder._stage_photos", side_effect=capture_stage),
        patch("ouestcharlie_toolkit.thumbnail_builder._call_image_proc", new=AsyncMock(
            return_value=(fake_grid, "B" * 22)
        )),
    ):
        await generate_partition_thumbnails(backend, "", photos)

    # _stage_photos receives photos sorted by content_hash.
    assert len(captured_entries) == 1
    hashes = [e.content_hash for e in captured_entries[0]]
    assert hashes == sorted(hashes)


@pytest.mark.asyncio
async def test_generate_partition_thumbnails_uses_tier_size(tmp_path: Path) -> None:
    """The tile_size passed to _call_image_proc must match the requested tier."""
    backend = LocalBackend(root=str(tmp_path))
    photos = [_fake_photo_entry("a.jpg", "sha256:" + "aa" * 32)]

    sizes_seen: list[int] = []

    async def capture_call(**kw):
        sizes_seen.append(kw["tile_size"])
        grid = ThumbnailGridLayout(cols=1, rows=1, tile_size=kw["tile_size"], photo_order=[])
        return grid, "C" * 22

    with (
        patch("ouestcharlie_toolkit.thumbnail_builder._find_image_proc_binary", return_value="fake-bin"),
        patch("ouestcharlie_toolkit.thumbnail_builder._stage_photos", new=AsyncMock(return_value=[])),
        patch("ouestcharlie_toolkit.thumbnail_builder._call_image_proc", side_effect=capture_call),
    ):
        await generate_partition_thumbnails(backend, "", photos, tier="thumbnail")
        await generate_partition_thumbnails(backend, "", photos, tier="preview")

    assert sizes_seen == [256, 1440]


@pytest.mark.asyncio
async def test_generate_partition_thumbnails_photo_order_in_grid(tmp_path: Path) -> None:
    """photo_order in the returned grid must contain all content hashes, sorted."""
    backend = LocalBackend(root=str(tmp_path))
    hashes = ["sha256:" + "cc" * 32, "sha256:" + "aa" * 32, "sha256:" + "bb" * 32]
    photos = [_fake_photo_entry(f"p{i}.jpg", h) for i, h in enumerate(hashes)]
    staged = [{"path": "/tmp/x", "ext": ".jpg", "orientation": 1, "content_hash": h}
              for h in sorted(hashes)]

    async def fake_call(**kw):
        order = [p["content_hash"] for p in kw["staged_photos"]]
        grid = ThumbnailGridLayout(cols=2, rows=2, tile_size=256, photo_order=order)
        return grid, "D" * 22

    with (
        patch("ouestcharlie_toolkit.thumbnail_builder._find_image_proc_binary", return_value="fake-bin"),
        patch("ouestcharlie_toolkit.thumbnail_builder._stage_photos", new=AsyncMock(return_value=staged)),
        patch("ouestcharlie_toolkit.thumbnail_builder._call_image_proc", side_effect=fake_call),
    ):
        grid, _ = await generate_partition_thumbnails(backend, "", photos)

    assert grid.photo_order == sorted(hashes)
