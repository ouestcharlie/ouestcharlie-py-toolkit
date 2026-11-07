"""XMP sidecars: file naming, parsing, serialization and the concurrency-safe store."""

from ouestcharlie_toolkit.xmp.parse import parse_xmp
from ouestcharlie_toolkit.xmp.paths import sidecar_candidates, xmp_path_for
from ouestcharlie_toolkit.xmp.serialize import serialize_xmp
from ouestcharlie_toolkit.xmp.store import XmpStore

__all__ = [
    "XmpStore",
    "parse_xmp",
    "serialize_xmp",
    "sidecar_candidates",
    "xmp_path_for",
]
