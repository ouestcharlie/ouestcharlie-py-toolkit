"""Test backend configuration and utilities."""

import tempfile
from pathlib import Path

import pytest

from ouestcharlie_toolkit.backend import backend_from_config
from ouestcharlie_toolkit.backends.local import LocalBackend
from ouestcharlie_toolkit.schema import ConfigurationError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_backend_with_files(tmpdir: Path) -> LocalBackend:
    """Create a LocalBackend rooted at tmpdir with a known set of files:
    - photo.jpg  (direct child)
    - notes.txt  (direct child)
    - sub/deep.jpg  (nested)
    """
    (tmpdir / "photo.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    (tmpdir / "notes.txt").write_text("hello")
    (tmpdir / "sub").mkdir()
    (tmpdir / "sub" / "deep.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    return LocalBackend(root=str(tmpdir))


def test_backend_from_config_local():
    """Test creating a local filesystem backend from config."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config = {"type": "filesystem", "root": tmpdir}
        backend = backend_from_config(config)

        assert isinstance(backend, LocalBackend)
        assert str(backend.root) == str(Path(tmpdir).resolve())


def test_backend_from_config_missing_type():
    """Test that missing backend type raises ConfigurationError."""
    config = {"root": "/tmp/test"}

    with pytest.raises(ConfigurationError, match="type"):
        backend_from_config(config)


def test_backend_from_config_unknown_type():
    """Test that unknown backend type raises ConfigurationError."""
    config = {"type": "unknown", "root": "/tmp/test"}

    with pytest.raises(ConfigurationError, match="Unsupported backend type"):
        backend_from_config(config)


def test_backend_from_config_missing_root():
    """Test that missing root path raises ConfigurationError."""
    config = {"type": "filesystem"}

    with pytest.raises(ConfigurationError, match="root"):
        backend_from_config(config)


def test_local_backend_initialization():
    """Test LocalBackend initialization."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend = LocalBackend(root=tmpdir)
        assert backend.root == Path(tmpdir).resolve()


def test_local_backend_nonexistent_root():
    """Test that LocalBackend raises error for nonexistent root."""
    with pytest.raises(FileNotFoundError, match="Backend root does not exist"):
        LocalBackend(root="/nonexistent/path/12345")


# ---------------------------------------------------------------------------
# LocalBackend.list_dirs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_dirs_returns_immediate_subdirs() -> None:
    """list_dirs returns only immediate subdirectories, not files or nested dirs."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend = _make_backend_with_files(Path(tmpdir))
        (Path(tmpdir) / "sub" / "nested").mkdir()
        dirs = await backend.list_dirs("")
        assert dirs == ["sub"]


@pytest.mark.asyncio
async def test_list_dirs_nonexistent_prefix() -> None:
    """list_dirs on a non-existent prefix returns an empty list."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend = LocalBackend(root=str(tmpdir))
        dirs = await backend.list_dirs("does_not_exist")
        assert dirs == []


# ---------------------------------------------------------------------------
# LocalBackend.list_files
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_files_no_filter() -> None:
    """list_files with no suffixes returns all direct-child files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend = _make_backend_with_files(Path(tmpdir))
        files = await backend.list_files("")
        paths = {f.path for f in files}
        assert paths == {"photo.jpg", "notes.txt"}


@pytest.mark.asyncio
async def test_list_files_with_suffixes() -> None:
    """list_files with a suffixes set returns only matching direct-child files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend = _make_backend_with_files(Path(tmpdir))
        files = await backend.list_files("", frozenset({".jpg"}))
        paths = {f.path for f in files}
        assert paths == {"photo.jpg"}


@pytest.mark.asyncio
async def test_list_files_empty_dir() -> None:
    """list_files on an empty directory returns an empty list."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend = LocalBackend(root=str(tmpdir))
        files = await backend.list_files("")
        assert files == []


@pytest.mark.asyncio
async def test_list_files_nonexistent_prefix() -> None:
    """list_files on a non-existent prefix returns an empty list."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend = LocalBackend(root=str(tmpdir))
        files = await backend.list_files("does_not_exist")
        assert files == []
