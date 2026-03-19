# Python Toolkit Low-Level Design

This document details the shared Python toolkit used by all OuEstCharlie agents. For technology selection rationale, see [agent_LLD_rationale.md](../ouestcharlie/agent/agent_LLD_rationale.md). For MCP tool definitions, see [controller_api.json](../ouestcharlie/controller_api.json).

## Overview

The Python toolkit (`ouestcharlie-toolkit`) is a shared library that provides four core capabilities to all agents:

1. **MCP integration** — MCP server lifecycle, tool registration, progress reporting, and logging
2. **Manifest read-edit with consistency** — hierarchical manifest traversal, atomic read-modify-write with optimistic concurrency
3. **XMP read-edit with consistency** — sidecar read-modify-write with optimistic concurrency and field-level semantics
4. **Thumbnail generation** — per-partition AVIF grid assembly delegated to the `avif-grid` Rust CLI

Agents import the toolkit and focus on their domain logic (indexing, enrichment, search). The toolkit handles protocol, storage, and consistency concerns.

## Package Structure

See [README.md](README.md) for the package structure and usage examples.

V1 scope: local filesystem backend only. The `backend.py` abstraction enables adding cloud backends (S3, GCS, ADLS Gen2) later without changing agent code.

## MCP Integration

### Server Lifecycle

`AgentBase` responsibilities:
1. Parse environment variables (`WOOF_BACKEND_CONFIG`, `WOOF_AGENT_TOKEN`)
2. Initialize the backend connection from config
3. Wrap FastMCP for MCP server lifecycle
4. Provide `progress(total)` factory for progress reporting
5. Provide `check_cancelled()` for cooperative cancellation
6. Provide `per_photo(photo, partition)` error isolation context manager

See [README.md](README.md) and implementation in [server.py](src/ouestcharlie/server.py) for usage examples.

## Backend Abstraction

The `Backend` protocol defines the storage operations: `read`, `write_conditional`, `write_new`, `list_files`, `exists`, `delete`. All paths are relative to the backend root.

`VersionToken` is backend-specific: `mtime` for local filesystem, `ETag` for S3/GCS/Azure Data Lake Storage Gen2, `generation` for GCS. It is opaque to callers.

### Local Filesystem Backend

Implementation uses:
- Async I/O via `asyncio.run_in_executor`
- Atomic write-to-temp-then-rename for `write_conditional` and `write_new`
- Version token based on `st_mtime_ns`

See [backends/local.py](src/ouestcharlie/backends/local.py) for implementation.

**Race window**: Between reading `mtime` and renaming, another writer could modify the file. For V1 (single-device, sequential agent execution by Woof), this window is acceptable. For future multi-agent concurrency, the backend can use `flock` or a compare-and-swap mechanism.

## Manifest Read-Edit with Consistency

### Data Model

Manifests are JSON files at well-known paths. The toolkit defines typed models: `PhotoEntry`, `PartitionSummary`, `LeafManifest`, `ParentManifest`.

See [schema.py](src/ouestcharlie/schema.py) for data class definitions and serialization helpers.

### Unknown Fields Preservation

Per the HLD schema evolution rules, unknown fields must be preserved:
- Manifest JSON is deserialized into typed data classes for known fields
- Unknown top-level and per-entry fields are captured in an `_extra: dict` attribute
- On serialization, known fields and `_extra` are merged back

### Read-Modify-Write with Optimistic Concurrency

`ManifestStore` provides `read_modify_write_leaf(partition, modify_fn)` that encapsulates the retry loop. Agents pass a `modify` function that transforms the manifest — the retry logic is invisible to them.

See [manifest.py](src/ouestcharlie/manifest.py) for implementation and [README.md](README.md) for usage examples.

### Parent Manifest Rebuilding

Parent manifests consolidate summaries from their children:
1. List all child manifest paths
2. Read each child manifest's summary (not full photo entries)
3. Merge summaries: union bloom filters, compute min/max dates, sum photo counts
4. Write the parent manifest with optimistic concurrency

## XMP Read-Edit with Consistency

### XMP Sidecar Format

XMP sidecars are XML files following the XMP specification (ISO 16684), with OuEstCharlie-specific fields in the `http://ouestcharlie.app/ns/1.0/` namespace.

Key fields:
- **Standard EXIF**: `exif:DateTimeOriginal`, `exif:GPS*`, `tiff:Make`, `tiff:Model`, `tiff:Orientation`
- **OuEstCharlie**: `ouestcharlie:contentHash`, `ouestcharlie:metadataVersion`, `ouestcharlie:schemaVersion`
- **Tags**: `dc:subject` contains enrichment tags (`ouestcharlie:faces/*`, `ouestcharlie:scene/*`) and album tags (`album/*`)

### Data Model and Operations

`XmpSidecar` data class with `_raw_xml` field to preserve unknown fields/namespaces for compatibility with Lightroom, darktable, ExifTool.

`XmpStore` provides `read_modify_write(photo_path, modify_fn)` with the same optimistic concurrency pattern as manifests.

See [xmp.py](src/ouestcharlie/xmp.py) for implementation and [README.md](README.md) for usage examples.

### Conflict-Free Merges

Since agents write non-overlapping fields (HLD § Consistency Model), most retry scenarios are simple merges:
- **Face enrichment** adds `ouestcharlie:faces/*` tags — does not touch `ouestcharlie:scene/*`
- **Scene enrichment** adds `ouestcharlie:scene/*` tags — does not touch `ouestcharlie:faces/*`
- **Housekeeping** writes `contentHash`, `metadataVersion`, EXIF fields — does not touch enrichment tags

### XMP Creation at Ingestion

When a new photo is indexed and no XMP sidecar exists:
1. Extract EXIF from the photo file (using `pyexiv2`)
2. Compute `SHA-256(file_bytes)` for the content hash
3. Build an `XmpSidecar` with extracted fields, `metadataVersion=1`, `schemaVersion=1`
4. Write using `write_new()` to avoid overwriting an existing sidecar

If an XMP sidecar already exists (created by Lightroom, darktable, etc.), the toolkit reads it, merges in OuEstCharlie-specific fields, and writes using the optimistic concurrency path. Existing third-party fields are preserved.

## Error Handling

Errors follow the three-category model from [controller_api.json](../../controller_api.json):

| Category | Toolkit behavior | Example |
|---|---|---|
| `transient` | Logged via MCP, agent continues with next item | File locked by another process |
| `permanent` | Logged via MCP, photo skipped | Corrupt EXIF, unsupported RAW format |
| `configuration` | Raised as exception, aborts the tool call | Backend root does not exist, invalid config |

`AgentBase` provides `per_photo(photo, partition)` context manager for error isolation without aborting the batch. See [server.py](src/ouestcharlie/server.py) for implementation.

## Thumbnail Generation

### Pipeline

Per partition, per tier (thumbnail 256 px / preview 1440 px):

```
Python: sort photos by content_hash → stage bytes to local tmpdir
  ↓  (one asyncio.create_subprocess_exec call)
avif-grid (Rust):
  rayon::par_iter — decode → apply orientation → resize → fit to square
  sequential      — YUV420 conversion → AVIF grid encoding (libavif)
  ↓
Python: read AVIF bytes from tmpdir → write to backend
```

No intermediate JPEG tile cache exists. Every call to `generate_partition_thumbnails` decodes all photos fresh, relying on Rust's speed and `rayon` parallelism to keep total latency acceptable.

### avif-grid JSON Protocol

**Stdin:**
```json
{
  "photos": [
    { "path": "/tmp/staged.jpg", "ext": ".jpg", "orientation": 6, "content_hash": "sha256:..." }
  ],
  "tile_size": 256,
  "fit": "crop",
  "quality": 55,
  "output": "/tmp/output.avif"
}
```

- `orientation` — TIFF orientation value 1–8 from the XMP sidecar; `null` means no transform.
- `fit` — `"crop"` (center-crop to square, used for thumbnails) or `"pad"` (letterbox with black, used for previews).
- Photos must be pre-sorted by `content_hash` (Python's responsibility) for stable tile indices.

**Stdout:**
```json
{ "cols": 32, "rows": 4, "tileSize": 256, "photoOrder": ["sha256:aaa...", ...] }
```

`photoOrder` reflects the tile order as received, so the caller can populate `ThumbnailGridLayout.photo_order` directly.

### Format Support and Platform Matrix

| Format | Cargo feature | System dep | Linux | macOS | Windows | iOS | Android |
|--------|--------------|-----------|:-----:|:-----:|:-------:|:---:|:-------:|
| JPEG, PNG, WebP, TIFF | *(default)* | None (pure Rust) | ✅ | ✅ | ✅ | ✅ | ✅ |
| RAW (CR2, NEF, ARW, DNG, RAF, ORF, RW2, PEF) | `raw` | None (pure Rust) | ✅ | ✅ | ✅ | ✅ | ✅ |
| HEIC/HEIF | `heic` | `libheif ≥ 1.17` | ✅ | ✅ | ⚠️ | — | — |

RAW and HEIC are compile-time features; the binary returns a clear error if a format is not compiled in.

**Notes:**
- JPEG/PNG/WebP/TIFF use the `image` crate (pure Rust, no system libraries, all targets).
- RAW uses `rawler` (pure Rust, no system libraries, all targets). The crate is pre-1.0; the version is pinned exactly.
- HEIC requires the system `libheif` library (`brew install libheif` / `apt install libheif-dev`). Windows support is possible but complex (vcpkg). iOS/Android require cross-compilation and are not supported in V1.
- `libavif` (required for AVIF grid encoding) follows the same model as HEIC — system library, not available on iOS/Android without significant effort.

### Grid Layout

- `cols = ceil(sqrt(n))`, `rows = ceil(n / cols)` — square-ish
- Last row padded with black tiles when `n` is not a multiple of `cols`
- AVIF quality: thumbnail 55, preview 60 (configurable via `AVIF_QUALITY`)

### Python Entry Points

`thumbnail_builder.py` exposes:
- `generate_partition_thumbnails(backend, partition, photo_entries)` — top-level orchestrator
- `_call_avif_grid(...)` — stages photos, calls the binary, writes AVIF to backend
- `_avif_path(partition, tier)` — canonical backend path for an AVIF file
- `_find_avif_grid_binary()` — resolves binary path via env var, `$PATH`, or dev build

## Dependencies

| Dependency | Purpose | Version constraint |
|---|---|---|
| `mcp` | MCP server SDK | `>=1.0` |
| `pyexiv2` | EXIF/XMP read-write (wraps Exiv2) | `>=2.8` |
| **avif-grid** (Rust binary) | Photo decode, resize, fit, AVIF assembly | built from `avif-grid/` |

## References

- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [MCP Specification (2025-11-25)](https://modelcontextprotocol.io/specification/2025-11-25)
- [pyexiv2](https://github.com/LeoHsiao1/pyexiv2) — EXIF/IPTC/XMP read-write
- [XMP Specification (ISO 16684)](https://www.iso.org/standard/75163.html)
- [HLD § Consistency Model](../../HLD.md) — optimistic concurrency design
- [controller_api.json](../../controller_api.json) — MCP tool definitions
