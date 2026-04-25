"""Tests for LocalBackend."""

import asyncio
import os
import tempfile
from pathlib import Path

import pytest

from ouestcharlie_toolkit.backend import (
    ConfigurationError,
    VersionConflictError,
    backend_from_config,
)
from ouestcharlie_toolkit.backends.local import LocalBackend

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


# ---------------------------------------------------------------------------
# Initialization + backend_from_config
# ---------------------------------------------------------------------------


def test_local_backend_initialization():
    with tempfile.TemporaryDirectory() as tmpdir:
        backend = LocalBackend(root=tmpdir)
        assert backend.root == Path(tmpdir).resolve()


def test_local_backend_nonexistent_root():
    with pytest.raises(FileNotFoundError, match="Backend root does not exist"):
        LocalBackend(root="/nonexistent/path/12345")


def test_backend_from_config_local():
    with tempfile.TemporaryDirectory() as tmpdir:
        backend = backend_from_config({"type": "filesystem", "root": tmpdir})
        assert isinstance(backend, LocalBackend)
        assert str(backend.root) == str(Path(tmpdir).resolve())


def test_backend_from_config_missing_root():
    with pytest.raises(ConfigurationError, match="root"):
        backend_from_config({"type": "filesystem"})


# ---------------------------------------------------------------------------
# list_dirs
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
# list_files
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
async def test_list_files_suffixes_case_insensitive() -> None:
    """list_files matches suffixes case-insensitively (e.g. .jpg matches PHOTO.JPG)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "PHOTO.JPG").write_bytes(b"\xff\xd8\xff\xd9")
        (Path(tmpdir) / "lower.jpg").write_bytes(b"\xff\xd8\xff\xd9")
        (Path(tmpdir) / "notes.txt").write_text("hello")
        backend = LocalBackend(root=tmpdir)
        files = await backend.list_files("", frozenset({".jpg"}))
        paths = {f.path for f in files}
        assert paths == {"PHOTO.JPG", "lower.jpg"}


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
# read
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
# write_new
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
# write_conditional
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
        await asyncio.sleep(0.01)
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
        await backend.write_conditional("f.txt", b"updated", version)
        data, _ = await backend.read("f.txt")
        assert data == b"updated"


# ---------------------------------------------------------------------------
# write_conditional — lock_dir placement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_conditional_lock_file_default_placement() -> None:
    """Without lock_dir the .lock file is created next to the target file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend = LocalBackend(root=tmpdir)
        version = await backend.write_new("f.txt", b"v1")
        await backend.write_conditional("f.txt", b"v2", version)
        assert (Path(tmpdir) / "f.txt.lock").exists()


@pytest.mark.asyncio
async def test_write_conditional_lock_file_in_lock_dir() -> None:
    """With lock_dir the .lock file is created inside the given directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / ".ouestcharlie").mkdir()
        backend = LocalBackend(root=tmpdir)
        version = await backend.write_new("f.txt", b"v1")
        await backend.write_conditional("f.txt", b"v2", version, ".ouestcharlie")
        assert (Path(tmpdir) / ".ouestcharlie" / "f.txt.lock").exists()
        assert not (Path(tmpdir) / "f.txt.lock").exists()


@pytest.mark.asyncio
async def test_write_conditional_lock_dir_created_if_missing() -> None:
    """lock_dir is created automatically if it does not yet exist."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend = LocalBackend(root=tmpdir)
        version = await backend.write_new("f.txt", b"v1")
        await backend.write_conditional("f.txt", b"v2", version, ".ouestcharlie")
        assert (Path(tmpdir) / ".ouestcharlie" / "f.txt.lock").exists()


@pytest.mark.asyncio
async def test_write_conditional_lock_dir_nested() -> None:
    """lock_dir works with nested paths (e.g. .ouestcharlie/partition)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "2024" / "July").mkdir(parents=True)
        backend = LocalBackend(root=tmpdir)
        version = await backend.write_new("2024/July/photo.xmp", b"<xmp/>")
        await backend.write_conditional(
            "2024/July/photo.xmp", b"<xmp2/>", version, ".ouestcharlie/2024/July"
        )
        assert (Path(tmpdir) / ".ouestcharlie" / "2024" / "July" / "photo.xmp.lock").exists()
        assert not (Path(tmpdir) / "2024" / "July" / "photo.xmp.lock").exists()


# ---------------------------------------------------------------------------
# exists / delete
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
# delete_dir
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_dir_removes_directory_and_contents() -> None:
    """delete_dir removes the directory and all its contents recursively."""
    with tempfile.TemporaryDirectory() as tmpdir:
        target = Path(tmpdir) / "meta" / "2024"
        target.mkdir(parents=True)
        (target / "manifest.json").write_bytes(b"{}")
        (target / "thumbnails.avif").write_bytes(b"\x00" * 16)
        (target / "sub").mkdir()
        (target / "sub" / "nested.json").write_bytes(b"{}")
        backend = LocalBackend(root=tmpdir)
        await backend.delete_dir("meta/2024")
        assert not target.exists()


@pytest.mark.asyncio
async def test_delete_dir_raises_for_missing_directory() -> None:
    """delete_dir raises FileNotFoundError if the directory does not exist."""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend = LocalBackend(root=tmpdir)
        with pytest.raises(FileNotFoundError):
            await backend.delete_dir("no_such_dir")


@pytest.mark.asyncio
async def test_delete_dir_raises_for_file_path() -> None:
    """delete_dir raises ValueError if the path points to a file, not a directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "a_file.txt").write_bytes(b"x")
        backend = LocalBackend(root=tmpdir)
        with pytest.raises(ValueError, match="Not a directory"):
            await backend.delete_dir("a_file.txt")


@pytest.mark.asyncio
async def test_delete_dir_sibling_directories_unaffected() -> None:
    """delete_dir removes only the target directory, leaving siblings intact."""
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "keep").mkdir()
        (Path(tmpdir) / "keep" / "data.json").write_bytes(b"{}")
        (Path(tmpdir) / "remove").mkdir()
        (Path(tmpdir) / "remove" / "data.json").write_bytes(b"{}")
        backend = LocalBackend(root=tmpdir)
        await backend.delete_dir("remove")
        assert not (Path(tmpdir) / "remove").exists()
        assert (Path(tmpdir) / "keep" / "data.json").read_bytes() == b"{}"


@pytest.mark.asyncio
async def test_delete_dir_nested_path() -> None:
    """delete_dir works on deeply nested paths."""
    with tempfile.TemporaryDirectory() as tmpdir:
        deep = Path(tmpdir) / "a" / "b" / "c"
        deep.mkdir(parents=True)
        (deep / "file.bin").write_bytes(b"\xff")
        backend = LocalBackend(root=tmpdir)
        await backend.delete_dir("a/b/c")
        assert not deep.exists()
        assert (Path(tmpdir) / "a" / "b").exists()


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
# Concurrency
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


@pytest.mark.asyncio
async def test_write_conditional_concurrent_serialised() -> None:
    """Concurrent write_conditional on the same file: all succeed sequentially
    or raise VersionConflictError — no data corruption, no silent overwrites."""
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
        unexpected = [
            r
            for r in results
            if isinstance(r, Exception) and not isinstance(r, VersionConflictError)
        ]
        assert not unexpected, f"Unexpected exceptions from write_conditional: {unexpected}"
        assert len(successes) >= 1
        assert len(successes) + len(conflicts) == 10
        content = (Path(tmpdir) / "shared.txt").read_bytes()
        assert content.startswith(b"writer-")
