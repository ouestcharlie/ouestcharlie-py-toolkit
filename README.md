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
├── hatch_build.py                # Build hook: compiles image-proc and bundles it in the wheel
├── image-proc/                   # Rust CLI: decode + resize + AVIF/JPEG assembly
│   ├── Cargo.toml
│   └── src/main.rs
└── src/
    └── ouestcharlie_toolkit/
        ├── bin/                  # Bundled image-proc binary (populated at build time)
        ├── schema.py             # Data models, exceptions, constants
        ├── backend.py            # Backend protocol
        ├── backends/
        │   └── local.py          # Local filesystem backend
        ├── manifest.py           # ManifestStore for manifest operations
        ├── xmp.py                # XmpStore for XMP sidecar operations
        ├── thumbnail_builder.py  # Thumbnail generation (delegates to image-proc)
        ├── progress.py           # ProgressReporter for MCP progress
        └── server.py             # AgentBase for MCP server lifecycle
```

## Installation

### From PyPI (recommended)

```bash
pip install ouestcharlie-toolkit
```

The `image-proc` binary is compiled and bundled inside the wheel at publish time — no Rust toolchain required at install time.

System prerequisites:
- **macOS**: `brew install inih` (required by pyexiv2 at runtime)
- **Linux/Windows**: no extra steps — pyexiv2 and image-proc wheels are self-contained

### From source (development)

Requires Rust and system libavif:

```bash
brew install libavif inih   # macOS
# apt install libavif-dev   # Linux

uv venv --python 3.13
uv pip install -e ".[dev]"
```

The `image-proc` binary is **not** compiled automatically in editable installs. Build it manually once:

```bash
cd image-proc && cargo build --release
# binary: image-proc/target/release/image-proc
```

The toolkit resolves the binary in this order:
1. `IMAGE_PROC_BINARY` environment variable
2. `bin/image-proc[.exe]` bundled inside the installed wheel
3. `image-proc` on `$PATH`
4. `image-proc/target/release/image-proc` relative to this repo (dev build)

### Optional features (RAW and HEIC)

To build with RAW or HEIC support, set env vars before `hatch build` or `cargo build`:

```bash
IMAGE_PROC_FEATURE_RAW=1 hatch build   # enables rawler (pure Rust RAW decoder)
IMAGE_PROC_FEATURE_HEIC=1 hatch build  # enables libheif-rs (requires brew install libheif)
```

## Dependencies

- `mcp>=1.0` — Official MCP Python SDK
- `pyexiv2>=2.8` — EXIF extraction from image files (wraps Exiv2); requires `brew install inih` on macOS
- `blake3>=1.0.8` — Fast content hashing

**image-proc** (Rust binary, bundled in the wheel) handles all image decoding, resizing, AVIF assembly, and JPEG preview generation.

XMP parsing and serialization use stdlib only and have no native dependencies.

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
- Thumbnail generation: per-partition AVIF grid via avif-grid Rust binary
  - Parallel decode (rayon) for JPEG, PNG, WebP, TIFF
  - Orientation correction (TIFF values 1–8)
  - Crop and pad fit modes
  - Stubbed RAW (`--features raw`) and HEIC (`--features heic`) support

### 📋 Future Work

- Cloud backend implementations (S3, GCS, ADLS Gen2, OneDrive, Kdrive)
- Bloom filter implementation for partition summaries

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
- [OuEstCharlie HLD](https://github.com/ouestcharlie/ouestcharlie/blob/master/HLD.md)
- [Agent LLD Rationale](https://github.com/ouestcharlie/ouestcharlie/blob/master/agent/agent_LLD_rationale.md)

## License

MIT license
