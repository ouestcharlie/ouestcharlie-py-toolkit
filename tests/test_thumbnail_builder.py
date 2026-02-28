"""Tests for thumbnail_builder — tile cache management and AVIF assembly."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image

from ouestcharlie_toolkit.backends.local import LocalBackend
from ouestcharlie_toolkit.schema import METADATA_DIR, ThumbnailGridLayout
from ouestcharlie_toolkit.thumbnail_builder import (
    ThumbnailResult,
    _avif_path,
    _find_avif_grid_binary,
    _fit_to_square,
    _tile_cache_path,
    assemble_avif,
    ensure_tile,
    generate_partition_thumbnails,
)

_SAMPLE_JPG = Path(__file__).parent / "sample-images" / "001.jpg"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _small_jpeg_bytes(width: int = 32, height: int = 24) -> bytes:
    """Return a minimal JPEG image as bytes."""
    img = Image.new("RGB", (width, height), color=(100, 150, 200))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _small_pil_image(width: int = 32, height: int = 24) -> Image.Image:
    return Image.new("RGB", (width, height), color=(100, 150, 200))


class _FakeAvifProcess:
    """Fake asyncio subprocess that writes a placeholder AVIF and returns grid JSON."""

    def __init__(self, cols: int = 2, rows: int = 1, tile_size: int = 256) -> None:
        self.returncode = 0
        self._cols = cols
        self._rows = rows
        self._tile_size = tile_size

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        if input:
            data = json.loads(input.decode())
            Path(data["output"]).write_bytes(b"FAKE_AVIF_CONTENT")
        stdout = json.dumps({
            "cols": self._cols,
            "rows": self._rows,
            "tileSize": self._tile_size,
        }).encode()
        return stdout, b""


class _FakeAvifProcessError:
    """Fake asyncio subprocess that simulates avif-grid failure."""

    returncode = 1

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        return b"", b"encoding failed: bad tile"


# ---------------------------------------------------------------------------
# _tile_cache_path
# ---------------------------------------------------------------------------


def test_tile_cache_path_root_partition() -> None:
    path = _tile_cache_path("", "sha256:" + "a" * 64, 256)
    assert path == f"{METADATA_DIR}/tile_cache/{'a' * 16}_256.jpg"


def test_tile_cache_path_sub_partition() -> None:
    path = _tile_cache_path("2024/2024-07", "sha256:" + "b" * 64, 1440)
    assert path == f"2024/2024-07/{METADATA_DIR}/tile_cache/{'b' * 16}_1440.jpg"


def test_tile_cache_path_strips_sha256_prefix() -> None:
    hex64 = "c" * 64
    path = _tile_cache_path("", f"sha256:{hex64}", 256)
    assert "sha256" not in path
    assert hex64[:16] in path


def test_tile_cache_path_trailing_slash_partition() -> None:
    path_with = _tile_cache_path("2024/2024-07/", "sha256:" + "d" * 64, 256)
    path_without = _tile_cache_path("2024/2024-07", "sha256:" + "d" * 64, 256)
    assert path_with == path_without


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
# _fit_to_square
# ---------------------------------------------------------------------------


def test_fit_crop_landscape_produces_square() -> None:
    """Landscape image (short_edge already == size) is center-cropped to a square."""
    from PIL import Image
    img = Image.new("RGB", (341, 256))  # landscape, short_edge = 256
    result = _fit_to_square(img, 256, "crop")
    assert result.size == (256, 256)


def test_fit_crop_portrait_produces_square() -> None:
    """Portrait image (short_edge already == size) is center-cropped to a square."""
    from PIL import Image
    img = Image.new("RGB", (256, 341))  # portrait, short_edge = 256
    result = _fit_to_square(img, 256, "crop")
    assert result.size == (256, 256)


def test_fit_crop_center_alignment() -> None:
    """Crop removes equal amounts from each side."""
    from PIL import Image
    # 400-wide, 256-tall → crop should remove 72px from each horizontal side.
    img = Image.new("RGB", (400, 256), (0, 0, 0))
    # Mark left/right edges with distinct colours so we can detect crop position.
    for y in range(256):
        img.putpixel((0, y), (255, 0, 0))    # far left: red
        img.putpixel((399, y), (0, 0, 255))  # far right: blue
    result = _fit_to_square(img, 256, "crop")
    assert result.size == (256, 256)
    # Left-most column of cropped image should NOT be red (72px removed).
    assert result.getpixel((0, 128)) != (255, 0, 0)
    # Right-most column should NOT be blue.
    assert result.getpixel((255, 128)) != (0, 0, 255)


def test_fit_pad_landscape_produces_square() -> None:
    """Landscape image is downscaled and padded to a square."""
    from PIL import Image
    img = Image.new("RGB", (341, 256))  # landscape
    result = _fit_to_square(img, 256, "pad")
    assert result.size == (256, 256)


def test_fit_pad_portrait_produces_square() -> None:
    """Portrait image is downscaled and padded to a square."""
    from PIL import Image
    img = Image.new("RGB", (256, 341))  # portrait
    result = _fit_to_square(img, 256, "pad")
    assert result.size == (256, 256)


def test_fit_pad_fills_with_black() -> None:
    """Padded regions are black."""
    from PIL import Image
    # 256×192 (4:3 landscape with short_edge already 256); long edge is 256 here.
    # Use a very wide image so there's obvious padding.
    img = Image.new("RGB", (256, 128), (200, 200, 200))  # grey content
    result = _fit_to_square(img, 256, "pad")
    assert result.size == (256, 256)
    # Top row should be black (padding).
    assert result.getpixel((128, 0)) == (0, 0, 0)
    # Bottom row should be black (padding).
    assert result.getpixel((128, 255)) == (0, 0, 0)


# ---------------------------------------------------------------------------
# _find_avif_grid_binary
# ---------------------------------------------------------------------------


def test_find_binary_uses_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AVIF_GRID_BINARY", "/custom/avif-grid")
    assert _find_avif_grid_binary() == "/custom/avif-grid"


def test_find_binary_uses_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AVIF_GRID_BINARY", raising=False)
    with patch("shutil.which", return_value="/usr/local/bin/avif-grid"):
        assert _find_avif_grid_binary() == "/usr/local/bin/avif-grid"


def test_find_binary_raises_when_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AVIF_GRID_BINARY", raising=False)
    with patch("shutil.which", return_value=None):
        with patch("pathlib.Path.exists", return_value=False):
            with pytest.raises(FileNotFoundError, match="avif-grid binary not found"):
                _find_avif_grid_binary()


# ---------------------------------------------------------------------------
# ensure_tile
# ---------------------------------------------------------------------------


@pytest.fixture()
def backend_with_photo(tmp_path: Path) -> LocalBackend:
    shutil.copy(_SAMPLE_JPG, tmp_path / "photo.jpg")
    return LocalBackend(root=str(tmp_path))


@pytest.mark.asyncio
async def test_ensure_tile_creates_tile_on_cache_miss(
    backend_with_photo: LocalBackend, tmp_path: Path
) -> None:
    content_hash = "sha256:" + "a" * 64

    with patch("ouestcharlie_toolkit.thumbnail_builder.decode_and_resize", return_value=_small_pil_image()):
        tile_path = await ensure_tile(
            backend=backend_with_photo,
            photo_path="photo.jpg",
            content_hash=content_hash,
            orientation=1,
            tile_size=256,
            partition="",
        )

    assert await backend_with_photo.exists(tile_path)
    tile_abs = tmp_path / tile_path
    assert tile_abs.exists()
    assert tile_abs.suffix == ".jpg"


@pytest.mark.asyncio
async def test_ensure_tile_returns_correct_path(backend_with_photo: LocalBackend) -> None:
    content_hash = "sha256:" + "b" * 64

    with patch("ouestcharlie_toolkit.thumbnail_builder.decode_and_resize", return_value=_small_pil_image()):
        tile_path = await ensure_tile(
            backend=backend_with_photo,
            photo_path="photo.jpg",
            content_hash=content_hash,
            orientation=1,
            tile_size=256,
            partition="",
        )

    expected = _tile_cache_path("", content_hash, 256)
    assert tile_path == expected


@pytest.mark.asyncio
async def test_ensure_tile_cache_hit_skips_decode(backend_with_photo: LocalBackend) -> None:
    """Second call to ensure_tile with the same content_hash skips decode+resize."""
    content_hash = "sha256:" + "c" * 64

    with patch(
        "ouestcharlie_toolkit.thumbnail_builder.decode_and_resize", return_value=_small_pil_image()
    ) as mock_decode:
        await ensure_tile(
            backend=backend_with_photo,
            photo_path="photo.jpg",
            content_hash=content_hash,
            orientation=1,
            tile_size=256,
            partition="",
        )
        call_count_after_first = mock_decode.call_count

        await ensure_tile(
            backend=backend_with_photo,
            photo_path="photo.jpg",
            content_hash=content_hash,
            orientation=1,
            tile_size=256,
            partition="",
        )

    assert mock_decode.call_count == call_count_after_first  # no extra call


@pytest.mark.asyncio
async def test_ensure_tile_different_sizes_cached_separately(
    backend_with_photo: LocalBackend,
) -> None:
    content_hash = "sha256:" + "d" * 64

    with patch("ouestcharlie_toolkit.thumbnail_builder.decode_and_resize", return_value=_small_pil_image()):
        path_256 = await ensure_tile(
            backend=backend_with_photo,
            photo_path="photo.jpg",
            content_hash=content_hash,
            orientation=1,
            tile_size=256,
            partition="",
        )
        path_1440 = await ensure_tile(
            backend=backend_with_photo,
            photo_path="photo.jpg",
            content_hash=content_hash,
            orientation=1,
            tile_size=1440,
            partition="",
        )

    assert path_256 != path_1440
    assert "256" in path_256
    assert "1440" in path_1440


@pytest.mark.asyncio
async def test_ensure_tile_sub_partition(tmp_path: Path) -> None:
    sub = tmp_path / "2024" / "July"
    sub.mkdir(parents=True)
    shutil.copy(_SAMPLE_JPG, sub / "photo.jpg")
    backend = LocalBackend(root=str(tmp_path))
    content_hash = "sha256:" + "e" * 64

    with patch("ouestcharlie_toolkit.thumbnail_builder.decode_and_resize", return_value=_small_pil_image()):
        tile_path = await ensure_tile(
            backend=backend,
            photo_path="2024/July/photo.jpg",
            content_hash=content_hash,
            orientation=1,
            tile_size=256,
            partition="2024/July",
        )

    assert tile_path.startswith("2024/July/")
    assert await backend.exists(tile_path)


# ---------------------------------------------------------------------------
# assemble_avif
# ---------------------------------------------------------------------------


@pytest.fixture()
def backend_with_tiles(tmp_path: Path) -> tuple[LocalBackend, list[str]]:
    """Backend pre-populated with two JPEG tile files."""
    backend = LocalBackend(root=str(tmp_path))

    tile_dir = tmp_path / METADATA_DIR / "tile_cache"
    tile_dir.mkdir(parents=True)

    tile_paths: list[str] = []
    for i in range(2):
        tile_name = f"{METADATA_DIR}/tile_cache/tile{i:02d}_256.jpg"
        (tmp_path / tile_name).write_bytes(_small_jpeg_bytes())
        tile_paths.append(tile_name)

    return backend, tile_paths


@pytest.mark.asyncio
async def test_assemble_avif_writes_output_to_backend(
    backend_with_tiles: tuple[LocalBackend, list[str]], tmp_path: Path
) -> None:
    backend, tile_paths = backend_with_tiles
    output_path = f"{METADATA_DIR}/thumbnails.avif"

    with patch(
        "asyncio.create_subprocess_exec",
        return_value=_FakeAvifProcess(cols=2, rows=1, tile_size=256),
    ):
        grid, content_hash = await assemble_avif(
            backend=backend,
            tile_paths=tile_paths,
            quality=55,
            output_path=output_path,
            avif_grid_binary="fake-avif-grid",
        )

    assert await backend.exists(output_path)
    assert grid.cols == 2
    assert grid.rows == 1
    assert grid.tile_size == 256


@pytest.mark.asyncio
async def test_assemble_avif_returns_sha256_hash(
    backend_with_tiles: tuple[LocalBackend, list[str]],
) -> None:
    backend, tile_paths = backend_with_tiles

    with patch(
        "asyncio.create_subprocess_exec",
        return_value=_FakeAvifProcess(),
    ):
        _, content_hash = await assemble_avif(
            backend=backend,
            tile_paths=tile_paths,
            quality=55,
            output_path=f"{METADATA_DIR}/thumbnails.avif",
            avif_grid_binary="fake-avif-grid",
        )

    assert content_hash.startswith("sha256:")
    expected = "sha256:" + hashlib.sha256(b"FAKE_AVIF_CONTENT").hexdigest()
    assert content_hash == expected


@pytest.mark.asyncio
async def test_assemble_avif_raises_on_nonzero_exit(
    backend_with_tiles: tuple[LocalBackend, list[str]],
) -> None:
    backend, tile_paths = backend_with_tiles

    with patch(
        "asyncio.create_subprocess_exec",
        return_value=_FakeAvifProcessError(),
    ):
        with pytest.raises(RuntimeError, match="avif-grid exited 1"):
            await assemble_avif(
                backend=backend,
                tile_paths=tile_paths,
                quality=55,
                output_path=f"{METADATA_DIR}/thumbnails.avif",
                avif_grid_binary="fake-avif-grid",
            )


@pytest.mark.asyncio
async def test_assemble_avif_overwrites_existing_output(
    backend_with_tiles: tuple[LocalBackend, list[str]], tmp_path: Path
) -> None:
    """assemble_avif should overwrite an existing AVIF file rather than fail."""
    backend, tile_paths = backend_with_tiles
    output_path = f"{METADATA_DIR}/thumbnails.avif"

    # Pre-create the output file.
    out_abs = tmp_path / METADATA_DIR / "thumbnails.avif"
    out_abs.parent.mkdir(parents=True, exist_ok=True)
    out_abs.write_bytes(b"OLD_CONTENT")

    with patch(
        "asyncio.create_subprocess_exec",
        return_value=_FakeAvifProcess(),
    ):
        _, content_hash = await assemble_avif(
            backend=backend,
            tile_paths=tile_paths,
            quality=55,
            output_path=output_path,
            avif_grid_binary="fake-avif-grid",
        )

    new_content = out_abs.read_bytes()
    assert new_content == b"FAKE_AVIF_CONTENT"
    assert content_hash == "sha256:" + hashlib.sha256(b"FAKE_AVIF_CONTENT").hexdigest()


# ---------------------------------------------------------------------------
# generate_partition_thumbnails
# ---------------------------------------------------------------------------


def _fake_photo_entry(filename: str, content_hash: str, orientation: int = 1):
    """Return a minimal object with the attributes PhotoEntry needs."""
    entry = MagicMock()
    entry.filename = filename
    entry.content_hash = content_hash
    entry.orientation = orientation
    return entry


@pytest.mark.asyncio
async def test_generate_partition_thumbnails_returns_result(tmp_path: Path) -> None:
    backend = LocalBackend(root=str(tmp_path))
    photos = [
        _fake_photo_entry("b.jpg", "sha256:" + "bb" * 32),
        _fake_photo_entry("a.jpg", "sha256:" + "aa" * 32),
    ]

    fake_grid = ThumbnailGridLayout(cols=2, rows=1, tile_size=256, photo_order=[])

    with (
        patch("ouestcharlie_toolkit.thumbnail_builder._find_avif_grid_binary", return_value="fake-bin"),
        patch("ouestcharlie_toolkit.thumbnail_builder.ensure_tile", new=AsyncMock(return_value="fake/tile.jpg")),
        patch("ouestcharlie_toolkit.thumbnail_builder.assemble_avif", new=AsyncMock(
            return_value=(fake_grid, "sha256:" + "cc" * 32)
        )),
    ):
        result = await generate_partition_thumbnails(backend, "", photos)

    assert isinstance(result, ThumbnailResult)
    assert result.thumbnails_hash.startswith("sha256:")
    assert result.previews_hash.startswith("sha256:")
    assert result.thumbnail_grid is not None
    assert result.preview_grid is not None


@pytest.mark.asyncio
async def test_generate_partition_thumbnails_tiles_sorted_by_hash(tmp_path: Path) -> None:
    """Tiles must be sorted by content_hash so indices are stable."""
    backend = LocalBackend(root=str(tmp_path))
    photos = [
        _fake_photo_entry("z.jpg", "sha256:" + "zz" * 32),
        _fake_photo_entry("a.jpg", "sha256:" + "aa" * 32),
        _fake_photo_entry("m.jpg", "sha256:" + "mm" * 32),
    ]

    captured_calls: list[list[str]] = []
    fake_grid = ThumbnailGridLayout(cols=2, rows=2, tile_size=256, photo_order=[])

    async def capture_assemble(backend, tile_paths, quality, output_path, avif_grid_binary):
        captured_calls.append(list(tile_paths))
        return fake_grid, "sha256:" + "00" * 32

    with (
        patch("ouestcharlie_toolkit.thumbnail_builder._find_avif_grid_binary", return_value="fake-bin"),
        patch("ouestcharlie_toolkit.thumbnail_builder.ensure_tile", new=AsyncMock(side_effect=lambda **kw: kw["content_hash"])),
        patch("ouestcharlie_toolkit.thumbnail_builder.assemble_avif", side_effect=capture_assemble),
    ):
        result = await generate_partition_thumbnails(backend, "", photos)

    # photo_order in the result should be sorted by content_hash ascending.
    expected_order = sorted(e.content_hash for e in photos)
    assert result.thumbnail_grid.photo_order == expected_order


@pytest.mark.asyncio
async def test_generate_partition_thumbnails_calls_both_tiers(tmp_path: Path) -> None:
    """Both thumbnail (256px) and preview (1440px) tiers must be generated."""
    backend = LocalBackend(root=str(tmp_path))
    photos = [_fake_photo_entry("a.jpg", "sha256:" + "aa" * 32)]

    sizes_seen: list[int] = []
    fake_grid = ThumbnailGridLayout(cols=1, rows=1, tile_size=256, photo_order=[])

    async def capture_ensure_tile(**kw):
        sizes_seen.append(kw["tile_size"])
        return f"fake/tile_{kw['tile_size']}.jpg"

    with (
        patch("ouestcharlie_toolkit.thumbnail_builder._find_avif_grid_binary", return_value="fake-bin"),
        patch("ouestcharlie_toolkit.thumbnail_builder.ensure_tile", side_effect=capture_ensure_tile),
        patch("ouestcharlie_toolkit.thumbnail_builder.assemble_avif", new=AsyncMock(
            return_value=(fake_grid, "sha256:" + "cc" * 32)
        )),
    ):
        await generate_partition_thumbnails(backend, "", photos)

    assert 256 in sizes_seen
    assert 1440 in sizes_seen


@pytest.mark.asyncio
async def test_generate_partition_thumbnails_photo_order_in_grid(tmp_path: Path) -> None:
    """photo_order in both grids must contain all content hashes, sorted."""
    backend = LocalBackend(root=str(tmp_path))
    hashes = ["sha256:" + "cc" * 32, "sha256:" + "aa" * 32, "sha256:" + "bb" * 32]
    photos = [_fake_photo_entry(f"p{i}.jpg", h) for i, h in enumerate(hashes)]

    fake_grid = ThumbnailGridLayout(cols=2, rows=2, tile_size=256, photo_order=[])

    with (
        patch("ouestcharlie_toolkit.thumbnail_builder._find_avif_grid_binary", return_value="fake-bin"),
        patch("ouestcharlie_toolkit.thumbnail_builder.ensure_tile", new=AsyncMock(return_value="t.jpg")),
        patch("ouestcharlie_toolkit.thumbnail_builder.assemble_avif", new=AsyncMock(
            return_value=(fake_grid, "sha256:" + "00" * 32)
        )),
    ):
        result = await generate_partition_thumbnails(backend, "", photos)

    assert result.thumbnail_grid.photo_order == sorted(hashes)
    assert result.preview_grid.photo_order == sorted(hashes)
