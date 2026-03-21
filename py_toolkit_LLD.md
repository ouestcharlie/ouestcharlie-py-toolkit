# Python Toolkit Low-Level Design

This document details the shared Python toolkit used by all OuEstCharlie agents. For technology selection rationale, see [agent_LLD_rationale.md](../ouestcharlie/agent/agent_LLD_rationale.md). For MCP tool definitions, see [controller_api.json](../ouestcharlie/controller_api.json).

## Overview

The Python toolkit (`ouestcharlie-toolkit`) is a shared library that provides four core capabilities to all agents:

1. **MCP integration** — MCP server lifecycle, tool registration, progress reporting, and logging
2. **Manifest read-edit with consistency** — hierarchical manifest traversal, atomic read-modify-write with optimistic concurrency
3. **XMP read-edit with consistency** — sidecar read-modify-write with optimistic concurrency and field-level semantics
4. **Image processing** — thumbnail AVIF grid assembly and on-demand JPEG preview generation, both delegated to the `image-proc` Rust CLI

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
2. Compute `content_hash(file_bytes)` (BLAKE3 128-bit, base64url, 22 chars) via `ouestcharlie_toolkit.hashing`
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

## Image Processing

The `image-proc` Rust CLI (in `image-proc/`) handles all pixel-level operations: decoding, orientation, resize, fit, and encoding. It is invoked via `asyncio.create_subprocess_exec` with a JSON payload on stdin and returns a JSON result on stdout. Two commands are supported, dispatched by the shape of the input (untagged serde enum).

### `avif_grid` command — thumbnail AVIF grid

Called by `generate_partition_thumbnails()` to produce the per-partition thumbnail AVIF container. Only the `"thumbnail"` tier is generated at indexing time (256 px, center-crop); the preview tier is replaced by lazy per-photo JPEG generation.

**Pipeline:**

```
Python: sort photos by content_hash → stage bytes to local tmpdir
  ↓  (one asyncio.create_subprocess_exec call, per tier)
image-proc avif_grid (Rust):
  rayon::par_iter — decode → apply orientation → resize → fit to square
  sequential      — YUV420 conversion → AVIF grid encoding (libavif)
  ↓
Python: read AVIF bytes from tmpdir → write to backend
```

**Stdin** (detected by presence of `"photos"` array):
```json
{
  "photos": [
    { "path": "/tmp/staged.jpg", "ext": ".jpg", "orientation": 6, "content_hash": "Kf3QzA2_nBcR8xYvLm1P9w" }
  ],
  "tile_size": 256,
  "fit": "crop",
  "quality": 55,
  "output": "/tmp/output.avif"
}
```

- `fit` — `"crop"` (center-crop to square, thumbnails) or `"pad"` (letterbox with black).
- Photos must be pre-sorted by `content_hash` (Python's responsibility) for stable tile indices.

**Stdout:**
```json
{ "cols": 32, "rows": 4, "tileSize": 256, "photoOrder": ["Kf3QzA2_nBcR8xYvLm1P9w", "aB1cD2eF3gH4i5jK6lM7nO", ...] }
```

### `jpeg_preview` command — on-demand preview JPEG

Called by `generate_preview_jpeg()` to produce a single-photo preview JPEG. Invoked by Wally's HTTP server on cache miss.

**Stdin** (detected by presence of `"photo"` object):
```json
{
  "photo": { "path": "/tmp/staged.cr2", "ext": ".cr2", "orientation": 1, "content_hash": "Kf3QzA2_nBcR8xYvLm1P9w" },
  "max_long_edge": 1440,
  "quality": 85,
  "output": "/tmp/preview.jpg"
}
```

- `max_long_edge` — the output JPEG's long edge is capped at this value; aspect ratio is preserved.
- `quality` — JPEG quality 1–95.

**Stdout:**
```json
{ "width": 1440, "height": 960 }
```

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
- AVIF quality: 55 for thumbnails (configurable via `AVIF_QUALITY`)

### Python Entry Points

`thumbnail_builder.py` exposes:
- `generate_partition_thumbnails(backend, partition, photo_entries, tiers=["thumbnail", "preview"])` — top-level orchestrator; Whitebeard passes `tiers=["thumbnail"]` to skip preview generation
- `generate_preview_jpeg(backend, partition, entry, max_long_edge=1440, jpeg_quality=85)` — generates and caches a single-photo JPEG preview; called by Wally's HTTP server
- `_call_image_proc(...)` — stages photos, calls the binary, writes AVIF to backend
- `_avif_path(partition, tier)` — canonical backend path for a thumbnail AVIF file
- `_preview_jpeg_path(partition, content_hash)` — canonical backend path for a preview JPEG (`{partition}/.ouestcharlie/previews/{content_hash}.jpg`)
- `_find_image_proc_binary()` — resolves binary path via `IMAGE_PROC_BINARY` env var, `$PATH`, or dev build (`image-proc/target/release/image-proc`)

## Dependencies

| Dependency | Purpose | Version constraint |
|---|---|---|
| `mcp` | MCP server SDK | `>=1.0` |
| `pyexiv2` | EXIF/XMP read-write (wraps Exiv2) | `>=2.8` |
| **image-proc** (Rust binary) | Photo decode, resize, fit, AVIF grid assembly, JPEG preview generation | built from `image-proc/` |

## References

- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [MCP Specification (2025-11-25)](https://modelcontextprotocol.io/specification/2025-11-25)
- [pyexiv2](https://github.com/LeoHsiao1/pyexiv2) — EXIF/IPTC/XMP read-write
- [XMP Specification (ISO 16684)](https://www.iso.org/standard/75163.html)
- [HLD § Consistency Model](../../HLD.md) — optimistic concurrency design
- [controller_api.json](../../controller_api.json) — MCP tool definitions
