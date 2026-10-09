"""Tests for AgentBase library-config parsing (WOOF_BACKEND_CONFIG)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ouestcharlie_toolkit.backend import ConfigurationError
from ouestcharlie_toolkit.server import AgentBase


def _make_agent(root: Path, monkeypatch: pytest.MonkeyPatch, **extra: Any) -> AgentBase:
    config = {"name": "testlib", "type": "filesystem", "path": str(root), **extra}
    monkeypatch.setenv("WOOF_BACKEND_CONFIG", json.dumps(config))
    return AgentBase("test-agent")


def test_excluded_tag_prefixes_default_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _make_agent(tmp_path, monkeypatch)
    assert agent.excluded_tag_prefixes == ["darktable"]
    assert agent.xmp_store.excluded_tag_prefixes == ("darktable",)


def test_excluded_tag_prefixes_from_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _make_agent(
        tmp_path, monkeypatch, excluded_tag_prefixes=["darktable", " Lightroom | Internal ", ""]
    )
    assert agent.excluded_tag_prefixes == ["darktable", "Lightroom|Internal"]
    assert agent.xmp_store.excluded_tag_prefixes == ("darktable", "Lightroom|Internal")


def test_excluded_tag_prefixes_empty_list_disables_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _make_agent(tmp_path, monkeypatch, excluded_tag_prefixes=[])
    assert agent.excluded_tag_prefixes == []


def test_excluded_tag_prefixes_must_be_a_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ConfigurationError):
        _make_agent(tmp_path, monkeypatch, excluded_tag_prefixes="darktable")
