"""Tests for SQL quoting."""

from __future__ import annotations

from ouestcharlie_toolkit.index import (
    sql_quote,
)

# ---------------------------------------------------------------------------
# sql_quote — SQL injection protection
# ---------------------------------------------------------------------------


def test_esc_clean_string_unchanged():
    assert sql_quote("hello") == "hello"


def test_esc_single_quote_doubled():
    assert sql_quote("it's") == "it''s"


def test_esc_multiple_quotes():
    assert sql_quote("a'b'c") == "a''b''c"


def test_esc_empty_string():
    assert sql_quote("") == ""
