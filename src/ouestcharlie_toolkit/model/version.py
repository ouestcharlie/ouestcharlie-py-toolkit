"""Schema version and XMP namespace of OuEstCharlie metadata."""

from __future__ import annotations

OUESTCHARLIE_NS = "http://ouestcharlie.app/ns/1.0/"
# Current schema version this software writes.
SCHEMA_VERSION = 6
# Oldest index schema version this software can still read and migrate in place
# (additive-only changes, e.g. new nullable Lance columns). An index whose
# version is within [LOWEST_SCHEMA_VERSION, SCHEMA_VERSION] is used as-is; older
# indexes need a full reindex, newer ones a software upgrade.
LOWEST_SCHEMA_VERSION = 6


def is_index_schema_compatible(version: int) -> bool:
    """True if an index at *version* can be used in place (no full reindex needed)."""
    return LOWEST_SCHEMA_VERSION <= version <= SCHEMA_VERSION
