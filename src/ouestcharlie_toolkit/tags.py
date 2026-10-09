"""Hierarchical tag paths.

A tag is a path whose levels are separated by ``|`` — the darktable and
Lightroom convention used by ``lr:hierarchicalSubject`` (e.g.
``Places|Europe|France|Paris``). A flat tag is a one-level path (``Family``).
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable

TAG_SEPARATOR = "|"

# Tag paths dropped from the index by default: darktable's automatic tags
# (darktable|format|jpg, darktable|changed, …).
DEFAULT_EXCLUDED_TAG_PREFIXES: tuple[str, ...] = ("darktable",)


def fold(term: str) -> str:
    """Case-insensitive comparison key: NFC, then lowercase.

    ``lower()`` rather than ``casefold()`` so that it agrees with DuckDB's
    ``lower(nfc_normalize(…))``, used to merge tag facets.
    """
    return unicodedata.normalize("NFC", term).lower()


def normalize_path(raw: str | None) -> str | None:
    """Trim each level and drop empty ones (``" a || b "`` → ``"a|b"``); None if nothing is left."""
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(TAG_SEPARATOR)]
    path = TAG_SEPARATOR.join(p for p in parts if p)
    return path or None


def normalize_paths(raws: Iterable[str | None]) -> list[str]:
    """Normalize several paths, dropping empty ones and duplicates (first-seen order)."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in raws:
        path = normalize_path(raw)
        if path is not None and path not in seen:
            seen.add(path)
            out.append(path)
    return out


def levels(path: str) -> list[str]:
    """``"a|b|c"`` → ``["a", "b", "c"]``."""
    return path.split(TAG_SEPARATOR)


def ancestors(path: str) -> list[str]:
    """Every prefix of *path*, itself included: ``"a|b|c"`` → ``["a", "a|b", "a|b|c"]``."""
    parts = levels(path)
    return [TAG_SEPARATOR.join(parts[: i + 1]) for i in range(len(parts))]


def leaf_paths(paths: Iterable[str]) -> list[str]:
    """Deduplicate *paths* and drop those that are a strict ancestor of another one.

    ``["a|b", "a|b|c", "d"]`` → ``["a|b|c", "d"]``: the ancestor is implied.
    """
    unique = normalize_paths(paths)
    implied = {a for p in unique for a in ancestors(p)[:-1]}
    return [p for p in unique if p not in implied]


def is_excluded(path: str, prefixes: Iterable[str]) -> bool:
    """True if *path* equals one of *prefixes* or sits below it (whole levels only, any case).

    ``darktable`` excludes ``darktable`` and ``Darktable|format``, not ``darktable-fans``.
    """
    key = fold(path)
    folded = (fold(p) for p in prefixes)
    return any(key == p or key.startswith(p + TAG_SEPARATOR) for p in folded)


def tag_terms(paths: Iterable[str]) -> list[str]:
    """Search terms for a photo: every ancestor path and level name, folded, deduplicated.

    Matching a folded filter value against these terms gives subtree matching
    for a path (``Places|Europe``) and any-level matching for a bare name
    (``Paris``), regardless of case.
    """
    out: list[str] = []
    seen: set[str] = set()
    for path in paths:
        for term in map(fold, ancestors(path) + levels(path)):
            if term not in seen:
                seen.add(term)
                out.append(term)
    return out


def flatten(paths: Iterable[str]) -> list[str]:
    """All levels of all *paths*, deduplicated, in first-seen order (``dc:subject`` form)."""
    out: list[str] = []
    seen: set[str] = set()
    for path in paths:
        for level in levels(path):
            if level not in seen:
                seen.add(level)
                out.append(level)
    return out
