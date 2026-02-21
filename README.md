# OuEstCharlie Python Toolkit

Shared Python library for building OuEstCharlie photo management agents.

## Overview

This toolkit provides three core capabilities:

1. **MCP integration** — MCP server lifecycle, tool registration, progress reporting, and logging
2. **Manifest read-edit with consistency** — hierarchical manifest traversal, atomic read-modify-write with optimistic concurrency
3. **XMP read-edit with consistency** — sidecar read-modify-write with optimistic concurrency and field-level semantics

## Package Structure

```
ouestcharlie-toolkit/
├── pyproject.toml
├── src/
│   └── ouestcharlie/
│       ├── __init__.py           # Package exports
│       ├── schema.py             # Data models, exceptions, constants
│       ├── backend.py            # Backend protocol
│       ├── backends/
│       │   ├── __init__.py
│       │   └── local.py          # Local filesystem backend
│       ├── manifest.py           # ManifestStore for manifest operations
│       ├── xmp.py                # XmpStore for XMP sidecar operations
│       ├── progress.py           # ProgressReporter for MCP progress
│       └── server.py             # AgentBase for MCP server lifecycle
```

## Installation

```bash
# Install in development mode
uv pip install -e .

# Install with dev dependencies
uv pip install -e ".[dev]"
```

## Dependencies

- `mcp>=1.0` — Official MCP Python SDK
- `pyexiv2>=2.8` — EXIF extraction from image files (wraps Exiv2); requires `brew install inih` on macOS
- `Pillow>=10.0` — Image processing
- `rawpy>=0.19` — RAW format support (wraps LibRaw)

XMP parsing and serialization (`parse_xmp`, `serialize_xmp`) use stdlib only and have no native dependencies.

## Usage

### Creating an Agent

```python
from ouestcharlie_toolkit import AgentBase

class HousekeepingAgent(AgentBase):
    def __init__(self):
        super().__init__(name="ouestcharlie-housekeeping", version="1.0.0")

        # Register tools using the FastMCP instance
        @self.mcp.tool()
        async def rebuild_partition(backend: str, partition: str, mode: str = "lazy"):
            """Rebuild partition manifest and thumbnails."""
            # Agent logic here
            photos = await self.backend.list_files(partition, suffix=".jpg")
            progress = self.progress(total=len(photos))

            for photo in photos:
                await self.check_cancelled()
                # Process photo...
                await progress.advance(message=f"Processing {photo.path}")

            return {"photosProcessed": len(photos), "errors": 0}

if __name__ == "__main__":
    agent = HousekeepingAgent()
    agent.run()  # Runs on stdio transport
```

### Working with Manifests

```python
from ouestcharlie_toolkit import ManifestStore, PhotoEntry

# Read-modify-write pattern
async def add_photo_to_manifest(store: ManifestStore, partition: str, photo: PhotoEntry):
    def modify(manifest):
        manifest.photos.append(photo)
        # Recompute summary...
        return manifest

    await store.read_modify_write_leaf(partition, modify)
```

### Working with XMP Sidecars

```python
from ouestcharlie_toolkit import XmpStore

# Read-modify-write pattern
async def add_face_tags(store: XmpStore, photo_path: str, faces: list[str]):
    def modify(xmp):
        for face in faces:
            tag = f"ouestcharlie:faces/{face}"
            if tag not in xmp.tags:
                xmp.tags.append(tag)
        return xmp

    await store.read_modify_write(photo_path, modify)
```

### Backend Configuration

The toolkit reads backend configuration from the `WOOF_BACKEND_CONFIG` environment variable:

```bash
export WOOF_BACKEND_CONFIG='{"type": "filesystem", "root": "/Users/alice/Photos"}'
```

## Implementation Status

### ✅ Completed

- Package structure and build configuration
- Data models (PhotoEntry, LeafManifest, ParentManifest, XmpSidecar)
- Backend protocol and local filesystem implementation
- ManifestStore with optimistic concurrency
- XmpStore with optimistic concurrency
- ProgressReporter with rate limiting
- AgentBase with MCP server lifecycle

### 🚧 Stub/TODO

The following functions have stub implementations that raise `NotImplementedError`:

- `xmp.parse_xmp()` — needs pyexiv2 implementation
- `xmp.serialize_xmp()` — needs pyexiv2 implementation
- `xmp.extract_exif()` — needs pyexiv2 implementation
- `manifest.rebuild_parent()` — needs bloom filter merging
- `schema` bloom filter serialization — currently uses hex encoding

### 📋 Future Work

- Cloud backend implementations (S3, GCS, ADLS Gen2, OneDrive, Kdrive)
- Bloom filter implementation for partition summaries
- Full pyexiv2 integration for XMP/EXIF operations
- Image thumbnail generation helpers
- Unit tests and integration tests

## Architecture

See [ouestcharlie/agent/agent_LLD_rationale.md](../ouestcharlie/agent/agent_LLD_rationale.md) for technology selection rationale.

Key design principles:

- **Optimistic concurrency** — All manifest and XMP writes use version tokens to detect conflicts
- **Unknown field preservation** — Schema evolution via `_extra` dict in dataclasses
- **Async throughout** — All I/O operations are async
- **Backend abstraction** — Swappable storage backends (local, S3, GCS, etc.)
- **MCP-native** — Built on FastMCP for clean agent implementation

## References

- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [MCP Specification](https://modelcontextprotocol.io/specification/2025-11-25)
- [pyexiv2](https://github.com/LeoHsiao1/pyexiv2)
- [OuEstCharlie HLD](../ouestcharlie/HLD.md)
- [Agent LLD Rationale](../ouestcharlie/agent/agent_LLD_rationale.md)
