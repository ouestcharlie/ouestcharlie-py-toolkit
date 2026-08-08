"""Tests for preview_builder — JPEG preview generation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from ouestcharlie_imageproc.image_proc import PersistentImageProc

from ouestcharlie_toolkit.backends.local import LocalBackend
from ouestcharlie_toolkit.preview_builder import generate_preview_jpeg
from ouestcharlie_toolkit.schema import preview_jpeg_path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_entry(filename: str, content_hash: str) -> MagicMock:
    entry = MagicMock()
    entry.filename = filename
    entry.content_hash = content_hash
    entry.searchable = {"orientation": 1}
    return entry


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


# ---------------------------------------------------------------------------
# generate_preview_jpeg
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_preview_jpeg_uses_persistent_proc(tmp_path: Path) -> None:
    """When image_proc is provided, generate_preview_jpeg uses it instead of spawning."""
    backend = LocalBackend(root=tmp_path)
    (tmp_path / "photo.jpg").write_bytes(b"FAKE_JPEG")
    entry = _fake_entry("photo.jpg", "Kf3QzA2nBcR8xYvLm1P9w")

    image_proc = AsyncMock(spec=PersistentImageProc)

    async def fake_request(payload: dict) -> dict:
        Path(payload["output"]).write_bytes(b"FAKE_PREVIEW_JPEG")
        return {"width": 1440, "height": 960}

    image_proc.request = fake_request

    cache_path = await generate_preview_jpeg(image_proc, backend, "", entry)

    assert cache_path.endswith(".jpg")
    data, _ = await backend.read(cache_path)
    assert data == b"FAKE_PREVIEW_JPEG"


@pytest.mark.asyncio
async def test_generate_preview_jpeg_video_uses_cover_frame(tmp_path: Path) -> None:
    """For a video, image-proc receives a decoded cover-frame JPEG, not the container."""
    from PIL import Image as PILImage

    backend = LocalBackend(root=tmp_path)
    _write_sample_video(tmp_path / "clip.mp4")
    entry = _fake_entry("clip.mp4", "Vf3QzA2nBcR8xYvLm1P9w")

    seen: dict = {}

    async def fake_request(payload: dict) -> dict:
        photo = payload["photo"]
        seen["ext"] = photo["ext"]
        seen["orientation"] = photo["orientation"]
        # image-proc must get a real, decodable image at the given path.
        with PILImage.open(photo["path"]) as img:
            seen["size"] = img.size
        Path(payload["output"]).write_bytes(b"FAKE_VIDEO_PREVIEW")
        return {"width": 64, "height": 48}

    image_proc = AsyncMock(spec=PersistentImageProc)
    image_proc.request = fake_request

    cache_path = await generate_preview_jpeg(image_proc, backend, "", entry)

    assert seen["ext"] == ".jpg"
    assert seen["orientation"] is None
    assert seen["size"] == (64, 48)
    data, _ = await backend.read(cache_path)
    assert data == b"FAKE_VIDEO_PREVIEW"


@pytest.mark.asyncio
async def test_generate_preview_jpeg_skips_generation_when_cached(tmp_path: Path) -> None:
    """If the preview already exists in the backend, generation is skipped entirely."""
    backend = LocalBackend(root=tmp_path)
    entry = _fake_entry("photo.jpg", "Kf3QzA2nBcR8xYvLm1P9w")
    cache_path = preview_jpeg_path("", entry.content_hash)

    await backend.write_new(cache_path, b"CACHED_PREVIEW")

    image_proc = AsyncMock(spec=PersistentImageProc)

    result = await generate_preview_jpeg(image_proc, backend, "", entry)

    assert result == cache_path
    image_proc.request.assert_not_called()
