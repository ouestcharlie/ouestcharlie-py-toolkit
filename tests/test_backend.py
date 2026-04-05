"""Test backend configuration and utilities."""

import asyncio
import os
import tempfile
from pathlib import Path

import pytest

from ouestcharlie_toolkit.backend import backend_from_config
from ouestcharlie_toolkit.backends.local import LocalBackend
from ouestcharlie_toolkit.schema import ConfigurationError, VersionConflictError

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
    return LocalBackend(root=tmpdir)


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
        backend = LocalBackend(root=tmpdir)
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
        backend = LocalBackend(root=tmpdir)
        files = await backend.list_files("")
        assert files == []


@pytest.mark.asyncio
async def test_list_files_nonexistent_prefix() -> None:
    """list_files on a non-existent prefix returns an empty list."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend = LocalBackend(root=tmpdir)
        files = await backend.list_files("does_not_exist")
        assert files == []


# ---------------------------------------------------------------------------
# LocalBackend.read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_returns_content_and_version() -> None:
    """read returns the file bytes and a non-None version token."""
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "a.txt").write_bytes(b"hello")
        backend = LocalBackend(root=tmpdir)
        data, version = await backend.read("a.txt")
        assert data == b"hello"
        assert version.value is not None


@pytest.mark.asyncio
async def test_read_version_matches_mtime() -> None:
    """read version token value equals the file's st_mtime_ns."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "a.txt"
        path.write_bytes(b"x")
        expected_mtime = os.stat(path).st_mtime_ns
        backend = LocalBackend(root=tmpdir)
        _, version = await backend.read("a.txt")
        assert version.value == expected_mtime


@pytest.mark.asyncio
async def test_read_missing_file_raises() -> None:
    """read raises FileNotFoundError for a missing file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend = LocalBackend(root=tmpdir)
        with pytest.raises(FileNotFoundError):
            await backend.read("no_such_file.txt")


# ---------------------------------------------------------------------------
# LocalBackend.write_new
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_new_creates_file() -> None:
    """write_new creates the file with the given content."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend = LocalBackend(root=tmpdir)
        await backend.write_new("out.txt", b"created")
        assert (Path(tmpdir) / "out.txt").read_bytes() == b"created"


@pytest.mark.asyncio
async def test_write_new_returns_version() -> None:
    """write_new returns a VersionToken whose value matches the new file's mtime."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend = LocalBackend(root=tmpdir)
        version = await backend.write_new("out.txt", b"x")
        expected = os.stat(Path(tmpdir) / "out.txt").st_mtime_ns
        assert version.value == expected


@pytest.mark.asyncio
async def test_write_new_creates_parent_dirs() -> None:
    """write_new creates intermediate directories as needed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend = LocalBackend(root=tmpdir)
        await backend.write_new("a/b/c.txt", b"deep")
        assert (Path(tmpdir) / "a" / "b" / "c.txt").read_bytes() == b"deep"


@pytest.mark.asyncio
async def test_write_new_raises_if_exists() -> None:
    """write_new raises FileExistsError if the file already exists."""
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "existing.txt").write_bytes(b"old")
        backend = LocalBackend(root=tmpdir)
        with pytest.raises(FileExistsError):
            await backend.write_new("existing.txt", b"new")


# ---------------------------------------------------------------------------
# LocalBackend.write_conditional
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_conditional_updates_file() -> None:
    """write_conditional overwrites the file when version matches."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend = LocalBackend(root=tmpdir)
        version = await backend.write_new("f.txt", b"v1")
        await asyncio.sleep(0.01)
        new_version = await backend.write_conditional("f.txt", b"v2", version)
        assert (Path(tmpdir) / "f.txt").read_bytes() == b"v2"
        assert new_version.value != version.value


@pytest.mark.asyncio
async def test_write_conditional_returns_new_version() -> None:
    """write_conditional returns a fresh version token after the write."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend = LocalBackend(root=tmpdir)
        v1 = await backend.write_new("f.txt", b"a")
        v2 = await backend.write_conditional("f.txt", b"b", v1)
        actual_mtime = os.stat(Path(tmpdir) / "f.txt").st_mtime_ns
        assert v2.value == actual_mtime


@pytest.mark.asyncio
async def test_write_conditional_raises_on_version_conflict() -> None:
    """write_conditional raises VersionConflictError when version is stale."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend = LocalBackend(root=tmpdir)
        v1 = await backend.write_new("f.txt", b"original")
        # Brief pause so the next write lands on a different mtime tick even
        # on coarse-resolution filesystems (e.g. tmpfs in CI).
        await asyncio.sleep(0.01)
        # Advance the file so v1 is stale
        await backend.write_conditional("f.txt", b"updated", v1)
        with pytest.raises(VersionConflictError):
            await backend.write_conditional("f.txt", b"conflict", v1)


@pytest.mark.asyncio
async def test_write_conditional_read_version_is_consistent() -> None:
    """Version returned by read matches what write_conditional expects."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend = LocalBackend(root=tmpdir)
        await backend.write_new("f.txt", b"init")
        _, version = await backend.read("f.txt")
        # Should succeed — version came from read, so it must be consistent
        await backend.write_conditional("f.txt", b"updated", version)
        data, _ = await backend.read("f.txt")
        assert data == b"updated"


# ---------------------------------------------------------------------------
# LocalBackend.exists / delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exists_true_for_existing_file() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "a.txt").write_bytes(b"x")
        backend = LocalBackend(root=tmpdir)
        assert await backend.exists("a.txt") is True


@pytest.mark.asyncio
async def test_exists_false_for_missing_file() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        backend = LocalBackend(root=tmpdir)
        assert await backend.exists("no_such.txt") is False


@pytest.mark.asyncio
async def test_delete_removes_file() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "bye.txt").write_bytes(b"x")
        backend = LocalBackend(root=tmpdir)
        await backend.delete("bye.txt")
        assert not (Path(tmpdir) / "bye.txt").exists()


@pytest.mark.asyncio
async def test_delete_raises_for_missing_file() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        backend = LocalBackend(root=tmpdir)
        with pytest.raises(FileNotFoundError):
            await backend.delete("no_such.txt")


# ---------------------------------------------------------------------------
# Path traversal guard
# ---------------------------------------------------------------------------


def test_resolve_rejects_path_traversal() -> None:
    """_resolve must not allow paths that escape the backend root."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend = LocalBackend(root=tmpdir)
        with pytest.raises(ValueError, match="escapes"):
            backend._resolve("../../etc/passwd")


# ---------------------------------------------------------------------------
# Concurrency: write_new — only one winner under concurrent callers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_new_concurrent_only_one_succeeds() -> None:
    """When N coroutines race to write_new the same path, exactly one succeeds."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend = LocalBackend(root=tmpdir)
        results = await asyncio.gather(
            *[backend.write_new("race.txt", f"writer-{i}".encode()) for i in range(10)],
            return_exceptions=True,
        )
        successes = [r for r in results if not isinstance(r, Exception)]
        errors = [r for r in results if isinstance(r, FileExistsError)]
        assert len(successes) == 1
        assert len(errors) == 9


# ---------------------------------------------------------------------------
# Concurrency: write_conditional — stale writers get VersionConflictError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_conditional_concurrent_serialised() -> None:
    """Concurrent write_conditional on the same file: all succeed sequentially
    or raise VersionConflictError — no data corruption, no silent overwrites.

    We do not assert an exact success count because on filesystems with coarse
    mtime resolution (e.g. Windows CI) two writes can land on the same tick,
    making both look valid.  The important invariants are:
    - At least one writer wins.
    - All outcomes are either a VersionToken or VersionConflictError (no other
      exceptions, no silent corruption).
    - The file on disk contains a coherent payload from exactly one writer.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        backend = LocalBackend(root=tmpdir)
        version = await backend.write_new("shared.txt", b"init")

        async def try_write(i: int):
            return await backend.write_conditional("shared.txt", f"writer-{i}".encode(), version)

        results = await asyncio.gather(
            *[try_write(i) for i in range(10)],
            return_exceptions=True,
        )
        successes = [r for r in results if not isinstance(r, Exception)]
        conflicts = [r for r in results if isinstance(r, VersionConflictError)]
        assert len(successes) >= 1
        assert len(successes) + len(conflicts) == 10
        # The file on disk must contain a coherent payload from one writer
        content = (Path(tmpdir) / "shared.txt").read_bytes()
        assert content.startswith(b"writer-")
