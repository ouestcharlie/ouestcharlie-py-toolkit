"""Tests for the searchable-field taxonomy, including sortability."""

from __future__ import annotations

from ouestcharlie_toolkit.fields import (
    PHOTO_FIELDS,
    SORTABLE_FIELD_TYPES,
    FieldType,
    is_sortable,
)


def _field(name: str):
    return next(f for f in PHOTO_FIELDS if f.name == name)


def test_scalar_fields_are_sortable() -> None:
    for name in ("dateTaken", "rating", "aperture", "make", "hasAudio"):
        assert is_sortable(_field(name)), name


def test_collection_gps_and_text_fields_are_not_sortable() -> None:
    for name in ("tags", "gps", "description"):
        assert not is_sortable(_field(name)), name


def test_sortable_types_exclude_non_scalar_types() -> None:
    for non_scalar in (
        FieldType.STRING_COLLECTION,
        FieldType.GPS_BOX,
        FieldType.TEXT,
        FieldType.DESCRIPTIVE,
    ):
        assert non_scalar not in SORTABLE_FIELD_TYPES
