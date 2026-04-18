"""Tests for image_proc — binary discovery, OneTimeImageProc, and PersistentImageProc."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouestcharlie_toolkit.image_proc import (
    OneTimeImageProc,
    PersistentImageProc,
    _find_image_proc_binary,
)

# ---------------------------------------------------------------------------
# Helpers
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


# ---------------------------------------------------------------------------
# _find_image_proc_binary
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
# OneTimeImageProc
# ---------------------------------------------------------------------------


class _FakeOneTimeProc:
    """Fake asyncio subprocess for OneTimeImageProc tests (communicate-based)."""

    def __init__(self, response: dict, returncode: int = 0, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._response = response
        self._stderr = stderr

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        return (json.dumps(self._response) + "\n").encode(), self._stderr


@pytest.mark.asyncio
async def test_one_time_image_proc_request_returns_result() -> None:
    fake = _FakeOneTimeProc({"cols": 2, "rows": 1, "tileSize": 256, "photoOrder": []})
    with patch("asyncio.create_subprocess_exec", return_value=fake):
        proc = OneTimeImageProc(binary="fake-image-proc")
        result = await proc.request({"tile_size": 256, "output": "/tmp/x.avif"})
    assert result == {"cols": 2, "rows": 1, "tileSize": 256, "photoOrder": []}


@pytest.mark.asyncio
async def test_one_time_image_proc_raises_on_nonzero_exit() -> None:
    fake = _FakeOneTimeProc({}, returncode=1, stderr=b"encoding failed")
    with patch("asyncio.create_subprocess_exec", return_value=fake):
        proc = OneTimeImageProc(binary="fake-image-proc")
        with pytest.raises(RuntimeError, match="image-proc exited 1"):
            await proc.request({})


@pytest.mark.asyncio
async def test_one_time_image_proc_raises_on_error_response() -> None:
    fake = _FakeOneTimeProc({"error": "bad photo format"})
    with patch("asyncio.create_subprocess_exec", return_value=fake):
        proc = OneTimeImageProc(binary="fake-image-proc")
        with pytest.raises(RuntimeError, match="bad photo format"):
            await proc.request({})


@pytest.mark.asyncio
async def test_one_time_image_proc_spawns_new_process_per_request() -> None:
    """Each request() call must spawn a fresh subprocess."""
    call_count = 0

    async def fake_spawn(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return _FakeOneTimeProc({"width": 800, "height": 600})

    proc = OneTimeImageProc(binary="fake-image-proc")
    with patch("asyncio.create_subprocess_exec", side_effect=fake_spawn):
        await proc.request({"output": "/tmp/a.jpg"})
        await proc.request({"output": "/tmp/b.jpg"})

    assert call_count == 2


# ---------------------------------------------------------------------------
# PersistentImageProc
# ---------------------------------------------------------------------------


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
        return live_proc

    proc_wrapper = PersistentImageProc(binary="fake-image-proc")
    proc_wrapper._proc = dead_proc  # inject dead process

    with patch("asyncio.create_subprocess_exec", side_effect=fake_spawn):
        result = await proc_wrapper.request({"output": "/tmp/x.jpg"})

    assert result == {"width": 800, "height": 600}
    assert call_count == 1
    await proc_wrapper.close()
