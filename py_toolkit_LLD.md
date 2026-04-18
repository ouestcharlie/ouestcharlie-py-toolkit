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

**Cross-process locking**: `write_conditional` holds two locks simultaneously for the duration of the stat-check + rename:

1. A per-path `threading.Lock` (intra-process thread safety) — required on macOS/BSD where `flock` is per-process and does not serialise threads within the same process.
2. A `_CrossProcessLock` on a `<filename>.lock` sidecar file (cross-process safety):
   - macOS/Linux: `fcntl.flock(LOCK_EX)` on the open fd.
   - Windows: `msvcrt.locking(LK_LOCK, 1)` on the open fd.

Callers pass a `lock_dir` (backend-relative path) to `write_conditional` so that `.lock` files are always created inside a `METADATA_DIR` (`.ouestcharlie/`) directory, never next to original photos. The lock files persist on disk — this is normal for `flock`-based locking; the OS-level lock releases when the fd is closed.

## Manifest Read-Edit with Consistency

### Data Model

Manifests are JSON files at well-known paths. The toolkit defines typed models: `PhotoEntry`, `ManifestSummary`, `RootSummary`, `LeafManifest`.

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

The `image-proc` Rust CLI (in `image-proc/`) handles all pixel-level operations: decoding, orientation, resize, fit, and encoding. Two commands are supported, dispatched by the shape of the input (untagged serde enum).

### Protocol — persistent newline-delimited JSON

`image-proc` runs as a persistent subprocess: it reads one JSON request per line from stdin and writes one JSON response per line to stdout. This eliminates per-request subprocess startup cost (significant on Windows).

- Requests and responses are newline-terminated JSON objects.
- Errors are returned in-band as `{"error": "…"}` — the process does not exit on error.
- The process exits when stdin is closed.

Two Python wrappers in `image_proc.py` implement this protocol:

| Class | Strategy | Use case |
|---|---|---|
| `OneTimeImageProc` | Spawns a fresh process per `request()` call; uses `communicate()` | `thumbnail_builder` — chunks already run in parallel via `asyncio.gather`, no shared process needed |
| `PersistentImageProc` | Keeps one process alive across calls; uses asyncio.Lock to serialise requests | Wally's `MediaMiddleware` — one process for all preview requests in the session |

`PersistentImageProc` restarts the process automatically if it crashes. Both classes expose the same interface:

```python
result: dict = await proc.request(payload_dict)
```

`PersistentImageProc` additionally implements `async def close()` and the async context manager protocol.

### `avif_grid` command — thumbnail AVIF grid

Called by `generate_partition_thumbnails()` via `OneTimeImageProc` to produce per-partition thumbnail AVIF chunks. Only the `"thumbnail"` tier is generated at indexing time (256 px, center-crop); the preview tier is replaced by lazy per-photo JPEG generation.

Photos are sorted by `content_hash`, then split into chunks of at most `GRID_MAX_PHOTOS = 64` entries each, yielding a maximum 8×8 grid per file. Chunks are encoded in parallel via `asyncio.gather`. Each AVIF file is named `thumbnails-{avif_hash}.avif`, where `avif_hash` is the 22-char BLAKE3 of the file's content — the filename is determined after encoding.

**Pipeline (per chunk):**

```
Python: sort photos by content_hash → split into chunks of ≤64
  ↓  (asyncio.gather — one coroutine per chunk, each in its own tmpdir)
  Per chunk:
    Python: stage chunk's photo bytes to tmpdir
      ↓  (OneTimeImageProc.request → asyncio.create_subprocess_exec)
    image-proc avif_grid (Rust):
      rayon::par_iter — decode → apply orientation → resize → fit to square
      sequential      — YUV420 conversion → AVIF grid encoding (libavif)
      ↓
    Python: hash bytes → name file → write to backend as thumbnails-{hash}.avif
```

**Request** (detected by presence of `"photos"` array):
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

**Response:**
```json
{ "cols": 32, "rows": 4, "tileSize": 256, "photoOrder": ["Kf3QzA2_nBcR8xYvLm1P9w", "aB1cD2eF3gH4i5jK6lM7nO", ...] }
```

### `jpeg_preview` command — on-demand preview JPEG

Called by `generate_preview_jpeg()` to produce a single-photo preview JPEG. Invoked by Wally's HTTP server on cache miss via a shared `PersistentImageProc` instance.

**Request** (detected by presence of `"photo"` object):
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

**Response:**
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

- `cols = ceil(sqrt(n))`, `rows = ceil(n / cols)` — square-ish, max 8×8 for 64 photos
- Last row padded with black tiles when `n` is not a multiple of `cols`
- AVIF quality: 55 for thumbnails (configurable via `AVIF_QUALITY`)
- Each chunk produces one `ThumbnailChunk(avif_path, avif_hash, grid)` stored in `LeafManifest.thumbnail_chunks`

### `ThumbnailChunk` schema

```python
ThumbnailChunk(
    avif_path="2024/Jul/.ouestcharlie/thumbnails-Kf3QzA2_nBcR8xYvLm1P9w.avif",
    avif_hash="Kf3QzA2_nBcR8xYvLm1P9w",
    grid=ThumbnailGridLayout(cols=8, rows=8, tile_size=256, photo_order=[...]),
)
```

### Python Modules

Image processing is split across three modules:

**`image_proc.py`** — subprocess management and binary discovery:
- `_find_image_proc_binary()` — resolves binary path via `IMAGE_PROC_BINARY` env var, bundled wheel binary, `$PATH`, or dev build (`image-proc/target/release/image-proc`)
- `OneTimeImageProc` — spawns a fresh process per `request()` call; used by `thumbnail_builder`
- `PersistentImageProc` — keeps one process alive with `asyncio.Lock` serialisation; used by Wally's `MediaMiddleware`

**`thumbnail_builder.py`** — AVIF grid generation:
- `generate_partition_thumbnails(backend, partition, photo_entries, tier="thumbnail")` — top-level orchestrator; returns `list[ThumbnailChunk]`
- `_call_image_proc(staged_photos, tile_size, fit, quality, tmpdir)` — calls image-proc via `OneTimeImageProc` for one chunk; returns `(ThumbnailGridLayout, avif_bytes)`
- `_stage_photos(backend, partition, photo_entries, tmpdir)` — reads photos from backend and writes them to a temp directory

**`preview_builder.py`** — on-demand JPEG preview generation:
- `generate_preview_jpeg(backend, partition, entry, image_proc)` — generates and caches a single-photo JPEG preview.

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
