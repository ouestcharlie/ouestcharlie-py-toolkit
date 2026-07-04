"""Tests for backend core types and factory error cases."""

import pytest

from ouestcharlie_toolkit.backend import (
    ConfigurationError,
    FileInfo,
    VersionConflictError,
    VersionToken,
    backend_from_config,
)

# ---------------------------------------------------------------------------
# VersionToken
# ---------------------------------------------------------------------------


def test_version_token():
    token = VersionToken(12345)
    assert token.value == 12345


def test_version_token_equality():
    token1 = VersionToken(12345)
    token2 = VersionToken(12345)
    token3 = VersionToken(54321)
    assert token1 == token2
    assert token1 != token3


# ---------------------------------------------------------------------------
# FileInfo
# ---------------------------------------------------------------------------


def test_file_info():
    token = VersionToken("etag-abc123")
    info = FileInfo(path="2024/photo.jpg", version=token)
    assert info.path == "2024/photo.jpg"
    assert info.version == token


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


def test_version_conflict_error():
    expected = VersionToken("v1")
    actual = VersionToken("v2")
    error = VersionConflictError("test.jpg", expected, actual)
    assert error.path == "test.jpg"
    assert error.expected == expected
    assert error.actual == actual
    assert "test.jpg" in str(error)
    assert "v1" in str(error)
    assert "v2" in str(error)


def test_configuration_error():
    with pytest.raises(ConfigurationError):
        raise ConfigurationError("Invalid config")


# ---------------------------------------------------------------------------
# backend_from_config — type-agnostic error cases
# ---------------------------------------------------------------------------


def test_backend_from_config_missing_type():
    with pytest.raises(ConfigurationError, match="type"):
        backend_from_config({"path": "/tmp/test"})


def test_backend_from_config_unknown_type():
    with pytest.raises(ConfigurationError, match="Unsupported backend type"):
        backend_from_config({"type": "unknown", "path": "/tmp/test"})
