"""Tests for preview_builder — JPEG preview generation."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouestcharlie_toolkit.backends.local import LocalBackend
from ouestcharlie_toolkit.image_proc import PersistentImageProc
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


# ---------------------------------------------------------------------------
# generate_preview_jpeg
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_preview_jpeg_uses_persistent_proc(tmp_path: Path) -> None:
    """When image_proc is provided, generate_preview_jpeg uses it instead of spawning."""
    backend = LocalBackend(root=tmp_path)
    (tmp_path / "photo.jpg").write_bytes(b"FAKE_JPEG")
    entry = _fake_entry("photo.jpg", "sha256:" + "ab" * 32)

    image_proc = AsyncMock(spec=PersistentImageProc)

    async def fake_request(payload: dict) -> dict:
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
    entry = _fake_entry("photo.jpg", "sha256:" + "cd" * 32)

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
    backend = LocalBackend(root=tmp_path)
    entry = _fake_entry("photo.jpg", "sha256:" + "ef" * 32)
    cache_path = preview_jpeg_path("", entry.content_hash)

    await backend.write_new(cache_path, b"CACHED_PREVIEW")

    image_proc = AsyncMock(spec=PersistentImageProc)

    result = await generate_preview_jpeg(backend, "", entry, image_proc=image_proc)

    assert result == cache_path
    image_proc.request.assert_not_called()
