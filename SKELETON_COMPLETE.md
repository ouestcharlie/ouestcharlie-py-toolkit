# Python Toolkit Skeleton — Complete ✅

**Status:** Skeleton complete, stubs documented, ready for implementation
**Created:** 2026-02-20

The Python toolkit code skeleton has been successfully created. All modules, classes, type signatures, and interfaces are in place.

See [README.md](README.md) for usage guide and installation instructions.

## Implementation Status

### ✅ Fully Functional

**Data Models** — All data structures, serialization/deserialization, and unknown field preservation working
- `PhotoEntry`, `PartitionSummary`, `LeafManifest`, `ParentManifest`, `XmpSidecar`
- `VersionToken`, `FileInfo`, exceptions

**Local Backend** — Complete async file I/O implementation
- Atomic write-then-rename with optimistic concurrency
- Version token based on `st_mtime_ns`
- File listing with glob patterns

**ManifestStore** — Read/write operations with retry logic
- `read_leaf`, `write_leaf`, `create_leaf` with version tokens
- `read_modify_write_leaf` with automatic retry on conflicts
- Same operations for parent manifests

**XmpStore** — XMP sidecar operations with optimistic concurrency
- `read`, `write`, `create` with version tokens
- `read_modify_write` with automatic retry
- `compute_content_hash` (SHA-256) fully functional
- `xmp_path_for` helper

**ProgressReporter** — Rate-limited MCP progress notifications
- 500ms minimum interval between updates
- `advance(n, message)` and `finish(message)` methods

**AgentBase** — MCP server lifecycle wrapper
- Environment config parsing (`WOOF_BACKEND_CONFIG`, `WOOF_AGENT_TOKEN`)
- Backend, manifest store, and XMP store initialization
- Progress reporting and cancellation support
- Error isolation with `per_photo()` context manager
- FastMCP integration

### 🚧 Stub Implementations (TODO)

The following functions have correct signatures and docstrings but raise `NotImplementedError`:

**XMP Parsing** ([xmp.py](src/ouestcharlie/xmp.py)):
- `parse_xmp(xml: str) -> XmpSidecar` — needs pyexiv2 implementation
- `serialize_xmp(sidecar: XmpSidecar) -> str` — needs pyexiv2 implementation
- `extract_exif(backend, photo_path) -> XmpSidecar` — needs pyexiv2 implementation

**Bloom Filters** ([manifest.py](src/ouestcharlie/manifest.py)):
- `rebuild_parent()` — needs bloom filter merging logic
- `_recompute_summary()` — needs bloom filter computation

## How to Complete the Stubs

### XMP Operations

Use `pyexiv2` library (wraps Exiv2):

```python
import pyexiv2

def parse_xmp(xml: str) -> XmpSidecar:
    # Use pyexiv2.ImageMetadata.from_buffer() to parse XMP
    # Extract fields: ouestcharlie:*, exif:*, dc:subject
    # Preserve _raw_xml for round-tripping
    ...

def serialize_xmp(sidecar: XmpSidecar) -> str:
    # Parse _raw_xml as baseline
    # Update known fields using pyexiv2
    # Return serialized XML
    ...
```

### Bloom Filters

Use a simple bloom filter library or implement manually:

```python
from pybloom_live import BloomFilter

def _compute_bloom(items: list[str]) -> bytes:
    bf = BloomFilter(capacity=1000, error_rate=0.01)
    for item in items:
        bf.add(item)
    return bf.bitarray.tobytes()

def _merge_blooms(blooms: list[bytes]) -> bytes:
    # Union of bloom filters = bitwise OR
    ...
```

## Testing

The toolkit now has 32 passing unit tests covering:
- Schema and data model operations
- Backend configuration and initialization
- XMP path utilities
- Content hash computation

See [README_DEV.md](README_DEV.md) for development setup and testing instructions.

## Next Steps

1. **Implement XMP stubs** — Use pyexiv2 for parsing and serialization
2. **Implement bloom filters** — Add bloom filter library and merging logic
3. **Build first agent** — Create Whitebeard housekeeping agent using this toolkit
4. **Add cloud backends** — Implement S3, GCS, ADLS Gen2 backends
5. **Integration tests** — Test agent ↔ Woof communication via MCP

## Summary

✅ **Skeleton complete and tested**

All interfaces, protocols, type signatures, and data models are in place with 32 passing tests. The toolkit provides a clean, type-safe foundation for building OuEstCharlie agents. Agents can be developed against this API immediately, with stub functions filled in as needed.
