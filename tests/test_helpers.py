"""Test helper functions and utilities."""

import pytest
import tempfile
from pathlib import Path
from ouestcharlie_toolkit import compute_content_hash
from ouestcharlie_toolkit.backends.local import LocalBackend


@pytest.mark.asyncio
async def test_compute_content_hash():
    """Test content hash computation from file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a test file
        test_file = Path(tmpdir) / "test.jpg"
        test_data = b"Hello, OuEstCharlie!"
        test_file.write_bytes(test_data)

        # Create backend and compute hash
        backend = LocalBackend(root=tmpdir)
        hash1 = await compute_content_hash(backend, "test.jpg")

        # Hash should be consistent
        hash2 = await compute_content_hash(backend, "test.jpg")
        assert hash1 == hash2

        # Hash should start with algorithm prefix
        assert hash1.startswith("sha256:")

        # Different data should produce different hashes
        test_file2 = Path(tmpdir) / "test2.jpg"
        test_file2.write_bytes(b"Different data")
        hash3 = await compute_content_hash(backend, "test2.jpg")
        assert hash1 != hash3


@pytest.mark.asyncio
async def test_compute_content_hash_empty():
    """Test content hash for empty file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create an empty file
        test_file = Path(tmpdir) / "empty.jpg"
        test_file.write_bytes(b"")

        # Create backend and compute hash
        backend = LocalBackend(root=tmpdir)
        hash_empty = await compute_content_hash(backend, "empty.jpg")

        assert hash_empty.startswith("sha256:")
        # SHA256 of empty string is known
        assert hash_empty == "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
