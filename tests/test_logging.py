"""Tests for ouestcharlie_toolkit.logging — platform-aware log setup."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from ouestcharlie_toolkit.logging import _default_log_dir, setup_logging


@pytest.fixture()
def clean_root_logger():
    """Restore root logger handlers and level after each test."""
    root = logging.getLogger()
    before_handlers = list(root.handlers)
    before_level = root.level
    yield
    for h in list(root.handlers):
        if h not in before_handlers:
            h.close()
            root.removeHandler(h)
    root.setLevel(before_level)


# ---------------------------------------------------------------------------
# _default_log_dir — platform dispatch
# ---------------------------------------------------------------------------


def test_default_log_dir_darwin():
    with (
        patch("ouestcharlie_toolkit.logging.platform.system", return_value="Darwin"),
        patch.object(sys, "platform", "darwin"),
    ):
        assert _default_log_dir("myagent") == Path.home() / "Library" / "Logs" / "myagent"


def test_default_log_dir_linux():
    with (
        patch("ouestcharlie_toolkit.logging.platform.system", return_value="Linux"),
        patch.object(sys, "platform", "linux"),
    ):
        assert _default_log_dir("myagent") == Path.home() / ".local" / "state" / "myagent"


def test_default_log_dir_linux_with_xdg(monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", "/custom/state")
    with (
        patch("ouestcharlie_toolkit.logging.platform.system", return_value="Linux"),
        patch.object(sys, "platform", "linux"),
    ):
        assert _default_log_dir("myagent") == Path("/custom/state/myagent")


def test_default_log_dir_windows(monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", "/win/appdata")
    with (
        patch("ouestcharlie_toolkit.logging.platform.system", return_value="Windows"),
        patch.object(sys, "platform", "win32"),
    ):
        assert _default_log_dir("myagent") == Path("/win/appdata/myagent/logs")


def test_default_log_dir_android():
    """Android check runs before platform.system(), which returns 'Linux' on Android."""
    with patch.object(sys, "platform", "android"):
        assert _default_log_dir("myagent") == Path.home() / "logs" / "myagent"


def test_default_log_dir_uses_agent_name():
    with (
        patch("ouestcharlie_toolkit.logging.platform.system", return_value="Darwin"),
        patch.object(sys, "platform", "darwin"),
    ):
        assert _default_log_dir("whitebeard").name == "whitebeard"


# ---------------------------------------------------------------------------
# setup_logging
# ---------------------------------------------------------------------------


def test_setup_logging_creates_log_file(tmp_path, monkeypatch, clean_root_logger):
    log_file = tmp_path / "agent.log"
    monkeypatch.setenv("TEST_AGENT_LOG", str(log_file))
    result = setup_logging("testagent", log_file_env_var="TEST_AGENT_LOG", redirect_stderr=False)
    assert log_file.exists()
    assert result == log_file


def test_setup_logging_creates_parent_dirs(tmp_path, monkeypatch, clean_root_logger):
    log_file = tmp_path / "nested" / "deep" / "agent.log"
    monkeypatch.setenv("TEST_AGENT_LOG", str(log_file))
    setup_logging("testagent", log_file_env_var="TEST_AGENT_LOG", redirect_stderr=False)
    assert log_file.exists()


def test_setup_logging_returns_path(tmp_path, monkeypatch, clean_root_logger):
    log_file = tmp_path / "agent.log"
    monkeypatch.setenv("TEST_AGENT_LOG", str(log_file))
    result = setup_logging("testagent", log_file_env_var="TEST_AGENT_LOG", redirect_stderr=False)
    assert result == log_file


def test_setup_logging_writes_to_log_file(tmp_path, monkeypatch, clean_root_logger):
    log_file = tmp_path / "agent.log"
    monkeypatch.setenv("TEST_AGENT_LOG", str(log_file))
    setup_logging("testagent", log_file_env_var="TEST_AGENT_LOG", redirect_stderr=False)

    logging.getLogger("test.write_check").info("hello from setup_logging test")
    for h in logging.getLogger().handlers:
        h.flush()

    assert "hello from setup_logging test" in log_file.read_text(encoding="utf-8")


def test_setup_logging_redirects_stderr(tmp_path, monkeypatch, clean_root_logger):
    log_file = tmp_path / "agent.log"
    monkeypatch.setenv("TEST_AGENT_LOG", str(log_file))
    original_stderr = sys.stderr
    try:
        setup_logging("testagent", log_file_env_var="TEST_AGENT_LOG", redirect_stderr=True)
        assert sys.stderr is not original_stderr
    finally:
        sys.stderr.close()
        sys.stderr = original_stderr


def test_setup_logging_no_redirect_leaves_stderr(tmp_path, monkeypatch, clean_root_logger):
    log_file = tmp_path / "agent.log"
    monkeypatch.setenv("TEST_AGENT_LOG", str(log_file))
    original_stderr = sys.stderr
    setup_logging("testagent", log_file_env_var="TEST_AGENT_LOG", redirect_stderr=False)
    assert sys.stderr is original_stderr
