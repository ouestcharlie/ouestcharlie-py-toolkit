"""Tests for CloudMountedBackend."""

import os
import tempfile
from pathlib import Path

import pytest

from ouestcharlie_toolkit.backend import ConfigurationError, backend_from_config
from ouestcharlie_toolkit.backends.cloud_mount import CloudMountedBackend
from ouestcharlie_toolkit.hashing import content_hash


@pytest.mark.asyncio
async def test_cloud_mount_read_returns_full_file() -> None:
    """read() returns correct bytes and a non-None version token for a normal file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "photo.jpg").write_bytes(b"full content")
        backend = CloudMountedBackend(root=tmpdir)
        data, version = await backend.read("photo.jpg")
    assert data == b"full content"
    assert version.value is not None


@pytest.mark.asyncio
async def test_cloud_mount_read_zero_byte_file() -> None:
    """read() returns b'' for a truly 0-byte file without error."""
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "empty.jpg").write_bytes(b"")
        backend = CloudMountedBackend(root=tmpdir)
        data, _ = await backend.read("empty.jpg")
    assert data == b""


@pytest.mark.asyncio
async def test_cloud_mount_read_retries_on_incomplete_then_succeeds(monkeypatch) -> None:
    """read() retries when len(data) < st_size and succeeds once the file is complete."""
    content = b"real content"
    call_count = 0
    real_fstat = os.fstat

    def fake_fstat(fd: int) -> os.stat_result:
        nonlocal call_count
        result = real_fstat(fd)
        call_count += 1
        # First pass: second fstat (st_size) reports the full logical size while
        # read() returned 0 bytes — simulates a dehydrated cloud placeholder.
        if call_count == 2:
            return os.stat_result(
                (
                    result.st_mode,
                    result.st_ino,
                    result.st_dev,
                    result.st_nlink,
                    result.st_uid,
                    result.st_gid,
                    len(content),
                    result.st_atime,
                    result.st_mtime,
                    result.st_ctime,
                )
            )
        return result

    async def no_sleep(_: float) -> None:
        pass

    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "photo.jpg").write_bytes(content)
        backend = CloudMountedBackend(root=tmpdir)
        monkeypatch.setattr(os, "fstat", fake_fstat)
        monkeypatch.setattr("asyncio.sleep", no_sleep)
        # First attempt: file appears dehydrated (0 bytes read, st_size = full size).
        # Second attempt: file is fully downloaded, real fstat matches data length.
        data, version = await backend.read("photo.jpg")

    assert data == content
    assert version.value is not None


@pytest.mark.asyncio
async def test_cloud_mount_read_raises_after_max_retries(monkeypatch) -> None:
    """read() raises OSError after exhausting retries on a persistently incomplete read."""
    content = b"real content"
    real_fstat = os.fstat

    def fake_fstat(fd: int) -> os.stat_result:
        result = real_fstat(fd)
        # Always report the full logical size regardless of bytes actually read.
        return os.stat_result(
            (
                result.st_mode,
                result.st_ino,
                result.st_dev,
                result.st_nlink,
                result.st_uid,
                result.st_gid,
                len(content) * 2,
                result.st_atime,
                result.st_mtime,
                result.st_ctime,
            )
        )

    async def no_sleep(_: float) -> None:
        pass

    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "photo.jpg").write_bytes(content)
        backend = CloudMountedBackend(root=tmpdir)
        monkeypatch.setattr(os, "fstat", fake_fstat)
        monkeypatch.setattr("asyncio.sleep", no_sleep)
        with pytest.raises(OSError, match="Incomplete read"):
            await backend.read("photo.jpg")


def test_backend_from_config_cloud_mount() -> None:
    """backend_from_config returns CloudMountedBackend for type 'cloud_mount'."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend = backend_from_config({"type": "cloud_mount", "path": tmpdir})
    assert isinstance(backend, CloudMountedBackend)


def test_backend_from_config_cloud_mount_missing_root() -> None:
    """backend_from_config raises ConfigurationError when root is absent."""
    with pytest.raises(ConfigurationError):
        backend_from_config({"type": "cloud_mount"})


@pytest.mark.asyncio
async def test_cloud_mount_local_path_inherited_from_local_backend() -> None:
    """local_path() is inherited from LocalBackend and returns the resolved mount path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend = CloudMountedBackend(root=tmpdir)
        result = await backend.local_path("photo.jpg")
    assert result == Path(tmpdir).resolve() / "photo.jpg"


@pytest.mark.asyncio
async def test_cloud_mount_content_hash_uses_retry_read() -> None:
    """content_hash() calls self.read(), which is overridden in CloudMountedBackend."""

    data = b"cloud file content"
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "photo.jpg").write_bytes(data)
        backend = CloudMountedBackend(root=tmpdir)
        result = await backend.content_hash("photo.jpg")
    assert result == content_hash(data)
