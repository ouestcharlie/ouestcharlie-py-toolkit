"""Tests for hierarchical tag path helpers (tags.py)."""

from ouestcharlie_toolkit.tags import (
    ancestors,
    flatten,
    fold,
    is_excluded,
    leaf_paths,
    levels,
    normalize_path,
    normalize_paths,
    tag_terms,
)


def test_normalize_path_trims_levels_and_drops_empty_ones():
    assert normalize_path(" Places | Europe ||France|") == "Places|Europe|France"
    assert normalize_path("Family") == "Family"


def test_normalize_path_empty_is_none():
    assert normalize_path("") is None
    assert normalize_path(None) is None
    assert normalize_path(" | ") is None


def test_normalize_paths_dedupes_in_first_seen_order():
    assert normalize_paths(["b", "a", " b ", "", None, "a|c"]) == ["b", "a", "a|c"]


def test_levels_and_ancestors():
    assert levels("a|b|c") == ["a", "b", "c"]
    assert ancestors("a|b|c") == ["a", "a|b", "a|b|c"]
    assert ancestors("a") == ["a"]


def test_leaf_paths_drops_implied_ancestors_and_duplicates():
    assert leaf_paths(["a|b", "a|b|c", "d", "a|b|c"]) == ["a|b|c", "d"]


def test_leaf_paths_keeps_siblings():
    assert leaf_paths(["a|b", "a|c"]) == ["a|b", "a|c"]


def test_leaf_paths_name_equal_to_level_is_not_an_ancestor():
    # "b" is a level of "a|b" but not its ancestor (ancestors start at the root).
    assert leaf_paths(["a|b", "b"]) == ["a|b", "b"]


def test_is_excluded_matches_whole_levels_only():
    assert is_excluded("darktable", ["darktable"])
    assert is_excluded("darktable|format|jpg", ["darktable"])
    assert not is_excluded("darktable-fans", ["darktable"])
    assert not is_excluded("Places|darktable", ["darktable"])


def test_is_excluded_deeper_prefix():
    assert is_excluded("a|b|c", ["a|b"])
    assert not is_excluded("a|c", ["a|b"])
    assert not is_excluded("a", ["a|b"])


def test_is_excluded_empty_prefixes_excludes_nothing():
    assert not is_excluded("darktable|format|jpg", [])


def test_is_excluded_ignores_case():
    assert is_excluded("Darktable|format|jpg", ["darktable"])
    assert is_excluded("darktable|changed", ["DARKTABLE"])
    assert is_excluded("lightroom|internal|x", ["Lightroom|Internal"])
    assert not is_excluded("Darktable-fans", ["darktable"])


def test_fold_lowercases_and_normalizes_to_nfc():
    assert fold("Places|Europe") == "places|europe"
    # precomposed "é" and "e" + combining acute accent fold to the same key
    assert fold("\u00c9t\u00e9") == fold("E\u0301te\u0301") == "\u00e9t\u00e9"


def test_tag_terms_are_ancestors_and_levels():
    assert tag_terms(["Places|Europe|Paris", "Family"]) == [
        "places",
        "places|europe",
        "places|europe|paris",
        "europe",
        "paris",
        "family",
    ]


def test_tag_terms_dedupe_after_folding():
    assert tag_terms(["Places|Europe", "places|europe|France", "Paris", "paris"]) == [
        "places",
        "places|europe",
        "europe",
        "places|europe|france",
        "france",
        "paris",
    ]


def test_tag_terms_empty():
    assert tag_terms([]) == []


def test_flatten_first_seen_order():
    assert flatten(["Places|Europe|France", "Places|Asia", "Family"]) == [
        "Places",
        "Europe",
        "France",
        "Asia",
        "Family",
    ]
