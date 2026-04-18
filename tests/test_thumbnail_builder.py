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
    PersistentImageProc,
    _call_image_proc,
    _find_image_proc_binary,
    generate_partition_thumbnails,
    generate_preview_jpeg,
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


_FAKE_HASH = "A" * 22


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
        stdout = json.dumps(
            {
                "cols": self._cols,
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
# _find_avif_grid_binary
# ---------------------------------------------------------------------------


def test_find_binary_uses_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMAGE_PROC_BINARY", "/custom/image-proc")
    assert _find_image_proc_binary() == "/custom/image-proc"


def test_find_binary_uses_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IMAGE_PROC_BINARY", raising=False)
    with (
        patch("pathlib.Path.exists", return_value=False),
        patch("shutil.which", return_value="/usr/local/bin/image-proc"),
    ):
        assert _find_image_proc_binary() == "/usr/local/bin/image-proc"


def test_find_binary_raises_when_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IMAGE_PROC_BINARY", raising=False)
    with (
        patch("shutil.which", return_value=None),
        patch("pathlib.Path.exists", return_value=False),
        pytest.raises(FileNotFoundError, match="image-proc binary not found"),
    ):
        _find_image_proc_binary()


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
    staged = [_staged(tmp_path, "sha256:" + "aa" * 32)]

    with patch(
        "asyncio.create_subprocess_exec",
        return_value=_FakeAvifProcess(cols=1, rows=1, tile_size=256),
    ):
        grid, avif_bytes = await _call_image_proc(
            staged_photos=staged,
            tile_size=256,
            fit="crop",
            quality=55,
            tmpdir=str(tmp_path),
            binary="fake-avif-grid",
        )

    assert avif_bytes == b"FAKE_AVIF_CONTENT"
    assert grid.cols == 1
    assert grid.rows == 1
    assert grid.tile_size == 256


@pytest.mark.asyncio
async def test_call_image_proc_passes_correct_json(tmp_path: Path) -> None:
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
            return json.dumps(
                {
                    "cols": 1,
                    "rows": 1,
                    "tileSize": 256,
                    "photoOrder": ["sha256:" + "cc" * 32],
                }
            ).encode(), b""

    with patch("asyncio.create_subprocess_exec", return_value=_CapturingProcess()):
        await _call_image_proc(
            staged_photos=staged,
            tile_size=256,
            fit="crop",
            quality=55,
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
async def test_call_image_proc_raises_on_nonzero_exit(tmp_path: Path) -> None:
    staged = [_staged(tmp_path, "sha256:" + "dd" * 32)]

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
            binary="fake-avif-grid",
        )


@pytest.mark.asyncio
async def test_call_image_proc_photo_order_in_grid(tmp_path: Path) -> None:
    """photo_order in the returned grid must reflect the photoOrder from Rust output."""
    hashes = ["sha256:" + "aa" * 32, "sha256:" + "bb" * 32]
    staged = [_staged(tmp_path, h) for h in hashes]

    with patch(
        "asyncio.create_subprocess_exec",
        return_value=_FakeAvifProcess(cols=2, rows=1, tile_size=256),
    ):
        grid, _ = await _call_image_proc(
            staged_photos=staged,
            tile_size=256,
            fit="crop",
            quality=55,
            tmpdir=str(tmp_path),
            binary="fake-avif-grid",
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

    fake_grid = ThumbnailGridLayout(cols=2, rows=1, tile_size=256, photo_order=[])

    with (
        patch(
            "ouestcharlie_toolkit.thumbnail_builder._find_image_proc_binary",
            return_value="fake-bin",
        ),
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
    fake_grid = ThumbnailGridLayout(cols=1, rows=1, tile_size=256, photo_order=[])

    with (
        patch(
            "ouestcharlie_toolkit.thumbnail_builder._find_image_proc_binary",
            return_value="fake-bin",
        ),
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

    fake_grid = ThumbnailGridLayout(cols=2, rows=2, tile_size=256, photo_order=[])

    with (
        patch(
            "ouestcharlie_toolkit.thumbnail_builder._find_image_proc_binary",
            return_value="fake-bin",
        ),
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

    # _stage_photos receives photos sorted by content_hash.
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
        grid = ThumbnailGridLayout(cols=1, rows=1, tile_size=kw["tile_size"], photo_order=[])
        return grid, b"FAKE"

    with (
        patch(
            "ouestcharlie_toolkit.thumbnail_builder._find_image_proc_binary",
            return_value="fake-bin",
        ),
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
        grid = ThumbnailGridLayout(cols=2, rows=2, tile_size=256, photo_order=order)
        return grid, b"FAKE"

    with (
        patch(
            "ouestcharlie_toolkit.thumbnail_builder._find_image_proc_binary",
            return_value="fake-bin",
        ),
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
        grid = ThumbnailGridLayout(cols=1, rows=1, tile_size=256, photo_order=[])
        return grid, f"FAKE{call_count}".encode()

    with (
        patch(
            "ouestcharlie_toolkit.thumbnail_builder._find_image_proc_binary",
            return_value="fake-bin",
        ),
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


# ---------------------------------------------------------------------------
# PersistentImageProc
# ---------------------------------------------------------------------------


def _make_fake_proc(responses: list[dict]):
    """Return a fake asyncio subprocess that replies with JSON lines from *responses*."""

    encoded = b"".join((json.dumps(r) + "\n").encode() for r in responses)
    stdout = AsyncMock()
    stdout.readline = AsyncMock(
        side_effect=[
            *(line + b"\n" for line in encoded.split(b"\n") if line),
            b"",  # EOF
        ]
    )
    stdin = MagicMock()
    stdin.write = MagicMock()
    stdin.drain = AsyncMock()
    stdin.is_closing = MagicMock(return_value=False)
    stdin.close = MagicMock()

    proc = MagicMock()
    proc.stdin = stdin
    proc.stdout = stdout
    proc.returncode = None
    proc.wait = AsyncMock(return_value=0)
    return proc


@pytest.mark.asyncio
async def test_persistent_image_proc_request_returns_result() -> None:
    fake_proc = _make_fake_proc([{"width": 1440, "height": 960}])
    with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
        async with PersistentImageProc(binary="fake-image-proc") as proc:
            result = await proc.request(
                {"photo": {}, "max_long_edge": 1440, "quality": 85, "output": "/tmp/x.jpg"}
            )
    assert result == {"width": 1440, "height": 960}


@pytest.mark.asyncio
async def test_persistent_image_proc_raises_on_error_response() -> None:
    fake_proc = _make_fake_proc([{"error": "bad photo format"}])
    with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
        async with PersistentImageProc(binary="fake-image-proc") as proc:
            with pytest.raises(RuntimeError, match="bad photo format"):
                await proc.request({})


@pytest.mark.asyncio
async def test_persistent_image_proc_raises_on_empty_stdout() -> None:
    """EOF on stdout (process died) raises RuntimeError."""
    fake_proc = _make_fake_proc([])
    with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
        async with PersistentImageProc(binary="fake-image-proc") as proc:
            with pytest.raises(RuntimeError, match="closed stdout"):
                await proc.request({})


@pytest.mark.asyncio
async def test_persistent_image_proc_restarts_after_crash() -> None:
    """If the process has exited, the next request spawns a fresh one."""
    dead_proc = _make_fake_proc([])
    dead_proc.returncode = 1  # simulate crashed process

    live_proc = _make_fake_proc([{"width": 800, "height": 600}])

    call_count = 0

    async def fake_spawn(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return live_proc  # always return live proc; dead_proc is pre-set on _proc

    proc_wrapper = PersistentImageProc(binary="fake-image-proc")
    proc_wrapper._proc = dead_proc  # inject dead process

    with patch("asyncio.create_subprocess_exec", side_effect=fake_spawn):
        result = await proc_wrapper.request({"output": "/tmp/x.jpg"})

    assert result == {"width": 800, "height": 600}
    assert call_count == 1  # spawned exactly once
    await proc_wrapper.close()


# ---------------------------------------------------------------------------
# generate_preview_jpeg — persistent image_proc path
# ---------------------------------------------------------------------------


def _fake_photo_entry_preview(filename: str, content_hash: str) -> MagicMock:
    entry = MagicMock()
    entry.filename = filename
    entry.content_hash = content_hash
    entry.searchable = {"orientation": 1}
    return entry


@pytest.mark.asyncio
async def test_generate_preview_jpeg_uses_persistent_proc(tmp_path: Path) -> None:
    """When image_proc is provided, generate_preview_jpeg uses it instead of spawning."""
    backend = LocalBackend(root=tmp_path)
    (tmp_path / "photo.jpg").write_bytes(b"FAKE_JPEG")
    entry = _fake_photo_entry_preview("photo.jpg", "sha256:" + "ab" * 32)

    image_proc = AsyncMock(spec=PersistentImageProc)

    async def fake_request(payload: dict) -> dict:
        # Write the expected output file so generate_preview_jpeg can read it.
        Path(payload["output"]).write_bytes(b"FAKE_PREVIEW_JPEG")
        return {"width": 1440, "height": 960}

    image_proc.request = fake_request

    cache_path = await generate_preview_jpeg(backend, "", entry, image_proc=image_proc)

    assert cache_path.endswith(".jpg")
    data, _ = await backend.read(cache_path)
    assert data == b"FAKE_PREVIEW_JPEG"


@pytest.mark.asyncio
async def test_generate_preview_jpeg_spawns_proc_when_none(tmp_path: Path) -> None:
    """When image_proc=None, generate_preview_jpeg falls back to spawning a subprocess."""
    backend = LocalBackend(root=tmp_path)
    (tmp_path / "photo.jpg").write_bytes(b"FAKE_JPEG")
    entry = _fake_photo_entry_preview("photo.jpg", "sha256:" + "cd" * 32)

    class _FakeProc:
        returncode = 0

        async def communicate(self, input=None):
            data = json.loads(input.decode())
            Path(data["output"]).write_bytes(b"SPAWNED_PREVIEW")
            return json.dumps({"width": 800, "height": 600}).encode(), b""

    with patch("asyncio.create_subprocess_exec", return_value=_FakeProc()):
        cache_path = await generate_preview_jpeg(backend, "", entry, image_proc=None)

    data, _ = await backend.read(cache_path)
    assert data == b"SPAWNED_PREVIEW"


@pytest.mark.asyncio
async def test_generate_preview_jpeg_skips_generation_when_cached(tmp_path: Path) -> None:
    """If the preview already exists in the backend, generation is skipped entirely."""
    from ouestcharlie_toolkit.schema import preview_jpeg_path

    backend = LocalBackend(root=tmp_path)
    entry = _fake_photo_entry_preview("photo.jpg", "sha256:" + "ef" * 32)
    cache_path = preview_jpeg_path("", entry.content_hash)

    await backend.write_new(cache_path, b"CACHED_PREVIEW")

    image_proc = AsyncMock(spec=PersistentImageProc)

    result = await generate_preview_jpeg(backend, "", entry, image_proc=image_proc)

    assert result == cache_path
    image_proc.request.assert_not_called()
