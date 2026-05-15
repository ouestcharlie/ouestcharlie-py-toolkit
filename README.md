# OuEstCharlie Python Toolkit

Shared Python library for building OuEstCharlie photo management agents.

## Overview

This toolkit provides four core capabilities:

1. **MCP integration** — MCP server lifecycle, tool registration, progress reporting, and logging
2. **Manifest read-edit with consistency** — hierarchical manifest traversal, atomic read-modify-write with optimistic concurrency
3. **XMP read-edit with consistency** — sidecar read-modify-write with optimistic concurrency and field-level semantics
4. **Image processing** — thumbnail AVIF grid assembly and on-demand JPEG preview generation, delegated to [`ouestcharlie-imageproc`](https://github.com/ouestcharlie/outestcharlie-imageproc)

## Package Structure

```
ouestcharlie-toolkit/
├── pyproject.toml
└── src/
    └── ouestcharlie_toolkit/
        ├── schema.py             # Data models, exceptions, constants
        ├── backend.py            # Backend protocol
        ├── backends/
        │   └── local.py          # Local filesystem backend
        ├── manifest.py           # ManifestStore for manifest operations
        ├── xmp.py                # XmpStore for XMP sidecar operations
        ├── thumbnail_builder.py  # Thumbnail generation (delegates to ouestcharlie-imageproc)
        ├── preview_builder.py    # On-demand JPEG preview (delegates to ouestcharlie-imageproc)
        ├── progress.py           # ProgressReporter for MCP progress
        └── server.py             # AgentBase for MCP server lifecycle
```

## Installation

### From PyPI (recommended)

```bash
pip install ouestcharlie-toolkit
```

`ouestcharlie-imageproc` (the Rust binary) is a separate package pulled in automatically. No Rust toolchain required at install time.

System prerequisites:
- **macOS**: `brew install inih brotli gettext` (required by pyexiv2 at runtime)
- **Linux/Windows**: no extra steps

### From source (development)

```bash
# For macOs on arm64 architecture, the full Python version is required e.g.: cpython-3.14.5-macos-aarch64-none
#  the version string is listed by `uv python list`
uv venv --python 3.13 
uv sync
```

`uv sync` uses the `[tool.uv.sources]` override to install `ouestcharlie-imageproc` from the adjacent `../outestcharlie-imageproc` checkout as an editable install (which compiles the Rust binary). Make sure that repo is checked out alongside this one.

## Running Tests

**Always use `.venv/bin/python -m pytest`** — do not use `.venv/bin/pytest` or a system `python`:

```bash
# Unit tests
.venv/bin/python -m pytest tests/ -v

# Run a specific file
.venv/bin/python -m pytest tests/test_photo.py -v --tb=short
```

Integration tests (real image-proc binary) are in `ouestcharlie-imageproc/tests_integration/`.

## Building a Wheel

The toolkit is pure Python — no Rust compilation required:

```bash
pip install hatch
hatch build
# produces dist/ouestcharlie_toolkit-*.whl (pure Python, any platform)
```

## Dependencies

- `mcp>=1.27` — Official MCP Python SDK
- `pyexiv2>=2.8` — EXIF extraction from image files (wraps Exiv2); requires `brew install inih` on macOS
- `blake3>=1.0.8` — Fast content hashing
- `ouestcharlie-imageproc>=1.0.0` — Rust coprocessor for image decode, resize, AVIF assembly, JPEG preview

XMP parsing and serialization use stdlib only and have no native dependencies.

## Usage

### Creating an Agent

```python
from ouestcharlie_toolkit import AgentBase

class HousekeepingAgent(AgentBase):
    def __init__(self):
        super().__init__(name="ouestcharlie-housekeeping", version="1.0.0")

        @self.mcp.tool()
        async def rebuild_partition(backend: str, partition: str, mode: str = "lazy"):
            """Rebuild partition manifest and thumbnails."""
            photos = await self.backend.list_files(partition, suffix=".jpg")
            progress = self.progress(total=len(photos))

            for photo in photos:
                await self.check_cancelled()
                await progress.advance(message=f"Processing {photo.path}")

            return {"photosProcessed": len(photos), "errors": 0}

if __name__ == "__main__":
    agent = HousekeepingAgent()
    agent.run()  # Runs on stdio transport
```

### Working with Manifests

```python
from ouestcharlie_toolkit import ManifestStore, PhotoEntry

async def add_photo_to_manifest(store: ManifestStore, partition: str, photo: PhotoEntry):
    def modify(manifest):
        manifest.photos.append(photo)
        return manifest

    await store.read_modify_write_leaf(partition, modify)
```

### Working with XMP Sidecars

```python
from ouestcharlie_toolkit import XmpStore

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

```bash
export WOOF_BACKEND_CONFIG='{"type": "filesystem", "root": "/Users/alice/Photos"}'
```

## Architecture

See [py_toolkit_LLD.md](py_toolkit_LLD.md) for the design and [agent_LLD_rationale.md](../ouestcharlie/agent/agent_LLD_rationale.md) for technology selection rationale.

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
- [OuEstCharlie HLD](https://github.com/ouestcharlie/ouestcharlie/blob/master/HLD.md)

## License

MIT license
