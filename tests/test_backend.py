"""Test backend configuration and utilities."""

import tempfile
from pathlib import Path

import pytest

from ouestcharlie_toolkit.backend import backend_from_config
from ouestcharlie_toolkit.backends.local import LocalBackend
from ouestcharlie_toolkit.schema import ConfigurationError


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
