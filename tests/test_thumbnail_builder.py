"""Tests for thumbnail_builder — AVIF grid generation pipeline."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouestcharlie_toolkit.backends.local import LocalBackend
from ouestcharlie_toolkit.hashing import content_hash as _content_hash
from ouestcharlie_toolkit.schema import (
    METADATA_DIR,
    ThumbnailChunk,
    ThumbnailGridLayout,
    thumbnail_avif_path,
)
from ouestcharlie_toolkit.thumbnail_builder import (
    GRID_MAX_PHOTOS,
    _call_image_proc,
    _stage_photos,
    delete_partition_thumbnails,
    generate_partition_thumbnails,
)

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


_FAKE_HASH = "A" * 22


def _write_sample_video(path: Path, *, frames: int = 15) -> None:
    """Synthesize a tiny mpeg4 MP4 for tests."""
    import av
    from PIL import Image as PILImage

    with av.open(str(path), "w") as container:
        vstream = container.add_stream("mpeg4", rate=30)
        vstream.width, vstream.height = 64, 48
        vstream.pix_fmt = "yuv420p"
        for i in range(frames):
            img = PILImage.new("RGB", (64, 48), ((i * 15) % 256, 40, 90))
            container.mux(vstream.encode(av.VideoFrame.from_image(img)))
        container.mux(vstream.encode())


class _FakeAvifProcess:
    """Fake asyncio subprocess that writes a placeholder AVIF and returns grid JSON."""

    def __init__(self, rows: int = 1, tile_size: int = 256) -> None:
        self.returncode = 0
        self._rows = rows
        self._tile_size = tile_size

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        photo_order: list[str] = []
        if input:
            data = json.loads(input.decode())
            Path(data["output"]).write_bytes(b"FAKE_AVIF_CONTENT")
            photo_order = [p["content_hash"] for p in data.get("photos", [])]
        stdout = json.dumps(
            {
                "rows": self._rows,
                "tileSize": self._tile_size,
                "photoOrder": photo_order,
            }
        ).encode()
        return stdout, b""


class _FakeAvifProcessError:
    """Fake asyncio subprocess that simulates avif-grid failure."""

    returncode = 1

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        return b"", b"encoding failed: bad photo"


# ---------------------------------------------------------------------------
# _stage_photos — video cover-frame staging
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage_photos_decodes_video_cover_to_jpeg(tmp_path: Path) -> None:
    """A video entry is staged as a decoded cover-frame JPEG, not the raw container."""
    from PIL import Image as PILImage

    _write_sample_video(tmp_path / "clip.mp4")
    backend = LocalBackend(root=tmp_path)
    entry = _fake_photo_entry("clip.mp4", "V" * 22, orientation=None)
    entry.searchable = {"media_type": "video"}

    staged_dir = tmp_path / "stage"
    staged_dir.mkdir()
    payload = await _stage_photos(backend, "", [entry], str(staged_dir))

    assert len(payload) == 1
    assert payload[0]["ext"] == ".jpg"
    assert payload[0]["orientation"] is None
    assert payload[0]["content_hash"] == "V" * 22
    jpeg = Path(payload[0]["path"])
    assert jpeg.exists() and jpeg.parent == staged_dir
    with PILImage.open(jpeg) as img:
        assert img.size == (64, 48)


@pytest.mark.asyncio
async def test_stage_photos_photo_uses_original_path(tmp_path: Path) -> None:
    """A photo entry references its original file directly (no re-staging)."""
    (tmp_path / "p.jpg").write_bytes(b"jpegbytes")
    backend = LocalBackend(root=tmp_path)
    entry = _fake_photo_entry("p.jpg", "P" * 22, orientation=6)

    payload = await _stage_photos(backend, "", [entry], str(tmp_path))

    assert payload[0]["ext"] == ".jpg"
    assert payload[0]["path"].endswith("p.jpg")
    assert payload[0]["orientation"] == 6


# ---------------------------------------------------------------------------
# thumbnail_avif_path
# ---------------------------------------------------------------------------


def test_avif_path_thumbnail_tier() -> None:
    assert (
        thumbnail_avif_path("2024/July", _FAKE_HASH, "thumbnail")
        == f"{METADATA_DIR}/2024/July/thumbnails-{_FAKE_HASH}.avif"
    )


def test_avif_path_preview_tier() -> None:
    assert (
        thumbnail_avif_path("2024/July", _FAKE_HASH, "preview")
        == f"{METADATA_DIR}/2024/July/previews-{_FAKE_HASH}.avif"
    )


def test_avif_path_root_partition() -> None:
    assert (
        thumbnail_avif_path("", _FAKE_HASH, "thumbnail")
        == f"{METADATA_DIR}/thumbnails-{_FAKE_HASH}.avif"
    )


# ---------------------------------------------------------------------------
# _call_image_proc — returns (ThumbnailGridLayout, bytes), no backend write
# ---------------------------------------------------------------------------


def _staged(
    tmp_path: Path, content_hash: str, ext: str = ".jpg", orientation: int | None = 1
) -> dict:
    """Build a pre-staged photo dict (as _stage_photos would produce)."""
    p = tmp_path / f"photo{ext}"
    p.write_bytes(b"FAKE_PHOTO")
    return {
        "path": str(p),
        "ext": ext,
        "orientation": orientation,
        "content_hash": content_hash,
    }


@pytest.mark.asyncio
async def test_call_image_proc_returns_bytes(tmp_path: Path) -> None:
    staged = [_staged(tmp_path, "Kf3QzA2nBcR8xYvLm1P9w")]

    with patch(
        "asyncio.create_subprocess_exec",
        return_value=_FakeAvifProcess(rows=1, tile_size=256),
    ):
        grid, avif_bytes = await _call_image_proc(
            staged_photos=staged,
            tile_size=256,
            fit="crop",
            quality=55,
            tmpdir=str(tmp_path),
        )

    assert avif_bytes == b"FAKE_AVIF_CONTENT"
    assert grid.rows == 1
    assert grid.tile_size == 256


@pytest.mark.asyncio
async def test_call_image_proc_passes_correct_json(tmp_path: Path) -> None:
    """The JSON payload sent to avif-grid must include photos, tile_size, fit, quality."""
    staged = [_staged(tmp_path, "Kf3QzA2nBcR8xYvLm1P9w", orientation=6)]
    captured: list[dict] = []

    class _CapturingProcess:
        returncode = 0

        async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
            if input:
                data = json.loads(input.decode())
                captured.append(data)
                Path(data["output"]).write_bytes(b"X")
            return json.dumps(
                {
                    "rows": 1,
                    "tileSize": 256,
                    "photoOrder": ["Kf3QzA2nBcR8xYvLm1P9w"],
                }
            ).encode(), b""

    with patch("asyncio.create_subprocess_exec", return_value=_CapturingProcess()):
        await _call_image_proc(
            staged_photos=staged,
            tile_size=256,
            fit="crop",
            quality=55,
            tmpdir=str(tmp_path),
        )

    assert len(captured) == 1
    payload = captured[0]
    assert payload["tile_size"] == 256
    assert payload["fit"] == "crop"
    assert payload["quality"] == 55
    assert len(payload["photos"]) == 1
    assert payload["photos"][0]["ext"] == ".jpg"
    assert payload["photos"][0]["orientation"] == 6
    assert payload["photos"][0]["content_hash"] == "Kf3QzA2nBcR8xYvLm1P9w"


@pytest.mark.asyncio
async def test_call_image_proc_raises_on_nonzero_exit(tmp_path: Path) -> None:
    staged = [_staged(tmp_path, "Kf3QzA2nBcR8xYvLm1P9w")]

    with (
        patch("asyncio.create_subprocess_exec", return_value=_FakeAvifProcessError()),
        pytest.raises(RuntimeError, match="image-proc exited 1"),
    ):
        await _call_image_proc(
            staged_photos=staged,
            tile_size=256,
            fit="crop",
            quality=55,
            tmpdir=str(tmp_path),
        )


@pytest.mark.asyncio
async def test_call_image_proc_photo_order_in_grid(tmp_path: Path) -> None:
    """photo_order in the returned grid must reflect the photoOrder from Rust output."""
    hashes = ["Kf3QzA2nBcR8xYvLm1P9w", "KfAbc123A2nBcR8xYvLm1P"]
    staged = [_staged(tmp_path, h) for h in hashes]

    with patch(
        "asyncio.create_subprocess_exec",
        return_value=_FakeAvifProcess(rows=1, tile_size=256),
    ):
        grid, _ = await _call_image_proc(
            staged_photos=staged,
            tile_size=256,
            fit="crop",
            quality=55,
            tmpdir=str(tmp_path),
        )

    assert grid.photo_order == hashes


# ---------------------------------------------------------------------------
# generate_partition_thumbnails
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_partition_thumbnails_returns_chunks(tmp_path: Path) -> None:
    backend = LocalBackend(root=tmp_path)
    photos = [
        _fake_photo_entry("b.jpg", "bb" * 32),
        _fake_photo_entry("a.jpg", "aa" * 32),
    ]

    fake_grid = ThumbnailGridLayout(rows=1, tile_size=256, photo_order=[])

    with (
        patch(
            "ouestcharlie_toolkit.thumbnail_builder._stage_photos",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "ouestcharlie_toolkit.thumbnail_builder._call_image_proc",
            new=AsyncMock(return_value=(fake_grid, b"FAKE_AVIF")),
        ),
    ):
        chunks = await generate_partition_thumbnails(backend, "", photos)

    assert len(chunks) == 1
    assert isinstance(chunks[0], ThumbnailChunk)
    assert chunks[0].grid is fake_grid
    assert chunks[0].avif_hash == _content_hash(b"FAKE_AVIF")
    assert len(chunks[0].avif_hash) == 22


@pytest.mark.asyncio
async def test_generate_partition_thumbnails_writes_avif_to_backend(
    tmp_path: Path,
) -> None:
    backend = LocalBackend(root=tmp_path)
    photos = [_fake_photo_entry("a.jpg", "aa" * 32)]
    fake_grid = ThumbnailGridLayout(rows=1, tile_size=256, photo_order=[])

    with (
        patch(
            "ouestcharlie_toolkit.thumbnail_builder._stage_photos",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "ouestcharlie_toolkit.thumbnail_builder._call_image_proc",
            new=AsyncMock(return_value=(fake_grid, b"FAKE_AVIF")),
        ),
    ):
        chunks = await generate_partition_thumbnails(backend, "", photos)

    assert await backend.exists(thumbnail_avif_path("", chunks[0].avif_hash))


@pytest.mark.asyncio
async def test_generate_partition_thumbnails_tiles_sorted_by_hash(
    tmp_path: Path,
) -> None:
    """Photos passed to _stage_photos must be sorted by content_hash."""
    backend = LocalBackend(root=tmp_path)
    photos = [
        _fake_photo_entry("z.jpg", "zz" * 32),
        _fake_photo_entry("a.jpg", "aa" * 32),
        _fake_photo_entry("m.jpg", "mm" * 32),
    ]

    captured_entries: list[list] = []

    async def capture_stage(backend, partition, photo_entries, tmpdir):
        captured_entries.append(list(photo_entries))
        return []

    fake_grid = ThumbnailGridLayout(rows=2, tile_size=256, photo_order=[])

    with (
        patch(
            "ouestcharlie_toolkit.thumbnail_builder._stage_photos",
            side_effect=capture_stage,
        ),
        patch(
            "ouestcharlie_toolkit.thumbnail_builder._call_image_proc",
            new=AsyncMock(return_value=(fake_grid, b"FAKE")),
        ),
    ):
        await generate_partition_thumbnails(backend, "", photos)

    assert len(captured_entries) == 1
    hashes = [e.content_hash for e in captured_entries[0]]
    assert hashes == sorted(hashes)


@pytest.mark.asyncio
async def test_generate_partition_thumbnails_uses_tier_size(tmp_path: Path) -> None:
    """The tile_size passed to _call_image_proc must match the requested tier."""
    backend = LocalBackend(root=tmp_path)
    photos = [_fake_photo_entry("a.jpg", "aa" * 32)]

    sizes_seen: list[int] = []

    async def capture_call(**kw):
        sizes_seen.append(kw["tile_size"])
        grid = ThumbnailGridLayout(rows=1, tile_size=kw["tile_size"], photo_order=[])
        return grid, b"FAKE"

    with (
        patch(
            "ouestcharlie_toolkit.thumbnail_builder._stage_photos",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "ouestcharlie_toolkit.thumbnail_builder._call_image_proc",
            side_effect=capture_call,
        ),
    ):
        await generate_partition_thumbnails(backend, "", photos, tier="thumbnail")
        await generate_partition_thumbnails(backend, "", photos, tier="preview")

    assert sizes_seen == [256, 1440]


@pytest.mark.asyncio
async def test_generate_partition_thumbnails_photo_order_in_grid(tmp_path: Path) -> None:
    """photo_order in the returned chunk grid must contain all content hashes, sorted."""
    backend = LocalBackend(root=tmp_path)
    hashes = ["cc" * 32, "aa" * 32, "bb" * 32]
    photos = [_fake_photo_entry(f"p{i}.jpg", h) for i, h in enumerate(hashes)]
    staged = [
        {"path": "/tmp/x", "ext": ".jpg", "orientation": 1, "content_hash": h}
        for h in sorted(hashes)
    ]

    async def fake_call(**kw):
        order = [p["content_hash"] for p in kw["staged_photos"]]
        grid = ThumbnailGridLayout(rows=2, tile_size=256, photo_order=order)
        return grid, b"FAKE"

    with (
        patch(
            "ouestcharlie_toolkit.thumbnail_builder._stage_photos",
            new=AsyncMock(return_value=staged),
        ),
        patch(
            "ouestcharlie_toolkit.thumbnail_builder._call_image_proc",
            side_effect=fake_call,
        ),
    ):
        chunks = await generate_partition_thumbnails(backend, "", photos)

    assert chunks[0].grid.photo_order == sorted(hashes)


@pytest.mark.asyncio
async def test_generate_partition_thumbnails_splits_into_chunks(tmp_path: Path) -> None:
    """More than GRID_MAX_PHOTOS photos must produce multiple chunks."""
    backend = LocalBackend(root=tmp_path)
    photos = [_fake_photo_entry(f"p{i}.jpg", f"{i:064x}") for i in range(GRID_MAX_PHOTOS + 1)]

    call_count = 0

    async def fake_call(**kw):
        nonlocal call_count
        call_count += 1
        grid = ThumbnailGridLayout(rows=1, tile_size=256, photo_order=[])
        return grid, f"FAKE{call_count}".encode()

    with (
        patch(
            "ouestcharlie_toolkit.thumbnail_builder._stage_photos",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "ouestcharlie_toolkit.thumbnail_builder._call_image_proc",
            side_effect=fake_call,
        ),
    ):
        chunks = await generate_partition_thumbnails(backend, "", photos)

    assert len(chunks) == 2


@pytest.mark.asyncio
async def test_generate_partition_thumbnails_idempotent(tmp_path: Path) -> None:
    """Calling generate_partition_thumbnails twice for the same photos must not raise.

    AVIF files are content-addressed: the same hash means the same content is already
    written, so a FileExistsError on the second write_new should be silently suppressed.
    """
    backend = LocalBackend(root=tmp_path)
    photos = [_fake_photo_entry("a.jpg", "aa" * 32)]
    fake_grid = ThumbnailGridLayout(rows=1, tile_size=256, photo_order=[])

    with (
        patch(
            "ouestcharlie_toolkit.thumbnail_builder._stage_photos",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "ouestcharlie_toolkit.thumbnail_builder._call_image_proc",
            new=AsyncMock(return_value=(fake_grid, b"FAKE_AVIF_IDEMPOTENT")),
        ),
    ):
        chunks1 = await generate_partition_thumbnails(backend, "", photos)
        chunks2 = await generate_partition_thumbnails(backend, "", photos)

    assert chunks1[0].avif_hash == chunks2[0].avif_hash
    assert await backend.exists(thumbnail_avif_path("", chunks1[0].avif_hash))


# ---------------------------------------------------------------------------
# delete_partition_thumbnails
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_partition_thumbnails_removes_thumbnail_avifs(tmp_path: Path) -> None:
    """delete_partition_thumbnails removes thumbnails-*.avif files from the metadata dir."""
    backend = LocalBackend(root=tmp_path)
    meta_dir = tmp_path / METADATA_DIR
    meta_dir.mkdir()
    thumb = meta_dir / "thumbnails-AAAAAAAAAAAAAAAAAAAAAA.avif"
    thumb.write_bytes(b"STALE")

    await delete_partition_thumbnails(backend, "")

    assert not thumb.exists()


@pytest.mark.asyncio
async def test_delete_partition_thumbnails_leaves_other_files(tmp_path: Path) -> None:
    """delete_partition_thumbnails does not touch previews or manifest files."""
    backend = LocalBackend(root=tmp_path)
    meta_dir = tmp_path / METADATA_DIR
    meta_dir.mkdir()
    preview = meta_dir / "previews-AAAAAAAAAAAAAAAAAAAAAA.avif"
    manifest = meta_dir / "manifest.json"
    preview.write_bytes(b"PREVIEW")
    manifest.write_bytes(b"{}")

    await delete_partition_thumbnails(backend, "")

    assert preview.exists()
    assert manifest.exists()


@pytest.mark.asyncio
async def test_delete_partition_thumbnails_missing_dir_is_noop(tmp_path: Path) -> None:
    """delete_partition_thumbnails does not raise when the metadata dir does not exist."""
    backend = LocalBackend(root=tmp_path)
    await delete_partition_thumbnails(backend, "")  # no exception


@pytest.mark.asyncio
async def test_delete_partition_thumbnails_subdirectory_partition(tmp_path: Path) -> None:
    """delete_partition_thumbnails targets the correct subdirectory for nested partitions."""
    backend = LocalBackend(root=tmp_path)
    meta_dir = tmp_path / METADATA_DIR / "2024" / "July"
    meta_dir.mkdir(parents=True)
    thumb = meta_dir / "thumbnails-AAAAAAAAAAAAAAAAAAAAAA.avif"
    thumb.write_bytes(b"STALE")

    await delete_partition_thumbnails(backend, "2024/July")

    assert not thumb.exists()
