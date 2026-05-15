# Python Toolkit Low-Level Design

This document details the shared Python toolkit used by all OuEstCharlie agents. For technology selection rationale, see [agent_LLD_rationale.md](../ouestcharlie/agent/agent_LLD_rationale.md). For MCP tool definitions, see [controller_api.json](../ouestcharlie/controller_api.json).

## Overview

The Python toolkit (`ouestcharlie-toolkit`) is a shared library that provides five core capabilities to all agents:

1. **MCP integration** — MCP server lifecycle, tool registration, progress reporting, and logging
2. **LanceDB columnar index** — schema-validated per-photo store at `.ouestcharlie/index.lance/`, upsert/delete operations, thumbnail location columns; partition-level stats computed by DuckDB aggregate queries over the index
3. **Manifest read-edit with consistency** — `summary.json` atomic read-modify-write with optimistic concurrency
4. **XMP read-edit with consistency** — sidecar read-modify-write with optimistic concurrency and field-level semantics
5. **Image processing** — thumbnail AVIF grid assembly and on-demand JPEG preview generation, delegated to `ouestcharlie-imageproc`

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

The `Backend` protocol defines the storage operations: `read`, `write_conditional`, `write_new`, `list_files`, `exists`, `delete`, `local_path`, `content_hash`. All paths are relative to the backend root.

`VersionToken` is backend-specific: `mtime` for local filesystem, `ETag` for S3/GCS/Azure Data Lake Storage Gen2, `generation` for GCS. It is opaque to callers.

`local_path(path)` returns the absolute local filesystem path for backends where the file lives on disk (local, cloud-mounted). Backend implementation is in charge of providing the local file copy.

`content_hash(path)` returns the canonical hash, URL safe. Default implementation reads via `read()` and computes the hash. Future remote backends (kDrive, OneDrive, etc.) can override to fetch the provider checksum from their REST API without downloading the file.

### Local Filesystem Backend

Implementation uses:
- Async I/O via `asyncio.run_in_executor`
- Atomic write-to-temp-then-rename for `write_conditional` and `write_new`
- Version token based on `st_mtime_ns`

See [backends/local.py](src/ouestcharlie/backends/local.py) for implementation.

**Cross-process locking**: `write_conditional` holds two locks simultaneously for the duration of the stat-check + rename:

1. A per-path `threading.Lock` (intra-process thread safety) — required on macOS/BSD where `flock` is per-process and does not serialize threads within the same process.
2. A `_CrossProcessLock` on a `<filename>.lock` sidecar file (cross-process safety):
   - macOS/Linux: `fcntl.flock(LOCK_EX)` on the open `fd`.
   - Windows: `msvcrt.locking(LK_LOCK, 1)` on the open `fd`.

Callers pass a `lock_dir` (backend-relative path) to `write_conditional` so that `.lock` files are always created inside a `METADATA_DIR` (`.ouestcharlie/`) directory, never next to original photos. The lock files persist on disk — this is normal for `flock`-based locking; the OS-level lock releases when the `fd` is closed.

### Cloud-Mounted Backend

`CloudMountedBackend` extends `LocalBackend` for FUSE and Windows CF API mounts (kDrive, OneDrive, GDrive, Dropbox). It overrides `read()` with an exponential-backoff retry loop to handle incomplete reads — Windows CF API may return partial data for dehydrated placeholder files. `local_path()` and `content_hash()` are inherited unchanged: photo-media tools (pyexiv2, image-proc) open the file directly via the mount path, letting FUSE handle on-demand download transparently.

## LanceDB Columnar Index (`lance_index.py`)

The LanceDB index replaces per-partition `manifest.json` files as the primary per-photo metadata store (schema version 3+). It is a single columnar table stored at `.ouestcharlie/index.lance/` within the backend root, containing one row per photo across all partitions.

### Schema

The PyArrow schema (`PHOTO_SCHEMA`) defines the table structure:

| Column | Type | Notes |
|---|---|---|
| `content_hash` | `string` | Primary key for upserts |
| `filename` | `string` | |
| `partition` | `string` | Relative path from backend root |
| `date_taken` | `timestamp(us)` | Nullable; stored timezone-naive |
| `utc_offset_minutes` | `int16` | Nullable; reserved for future use |
| `rating` | `int32` | Nullable |
| `width`, `height` | `int32` | Nullable |
| `orientation` | `int32` | Nullable |
| `make`, `model` | `string` | Nullable |
| `tags` | `list<string>` | Empty list when absent |
| `gps_lat`, `gps_lon` | `float64` | Nullable flat columns (not a struct) |
| `metadata_version` | `int64` | |
| `xmp_version_token` | `string` | Used for change detection |
| `thumbnail_avif_hash` | `string` | Nullable; identifies the AVIF chunk file |
| `thumbnail_tile_index` | `int16` | Nullable; row-major position in the grid |
| `_last_update` | `timestamp(us)` | Set to `datetime.now(utc)` on each write |

GPS and thumbnail location use **flat nullable columns** rather than structs. LanceDB returns null struct fields as default-valued dicts, not `None`, which would require extra handling at read sites.

### Partition Summary (`partition_summary.py`)

`compute_partition_summary(lance_index, partition)` runs a single DuckDB aggregate query over the LanceDB index to compute the `ManifestSummary` for a partition — photo count, date range, GPS bounding box, rating range. This replaces in-memory computation from the photo list: the index is the source of truth, so stats are derived from it directly after upsert.

## Manifest Read-Edit with Consistency

`ManifestStore` manages `summary.json` — the backend-wide flat index of all partitions. Per-photo metadata is stored in the LanceDB index; `summary.json` holds partition-level statistics and the schema version.

### Data Model

The toolkit defines typed models: `PhotoEntry`, `ManifestSummary`, `RootSummary`.

`schema.py` carries the canonical `SCHEMA_VERSION` constant (currently `3`) and `LANCE_INDEX_SUBDIR` (`"index.lance"`).

### Read-Modify-Write with Optimistic Concurrency

`ManifestStore.upsert_partition_in_summary(summary)` encapsulates the read-modify-write retry loop for `summary.json`. Agents pass a `ManifestSummary` — the retry logic is invisible to them.

### Unknown Fields Preservation

Unknown fields in `summary.json` are captured in `_extra: dict` and merged back on serialization, following HLD schema evolution rules.

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
1. Compute the content hash via `backend.content_hash(path)` — BLAKE3 truncated to 128 bits, base64url-encoded without padding, 22 characters. Raises `ValueError` for empty files. Future remote backends can override to fetch the provider checksum from their REST API without downloading the file.
2. Extract EXIF using `pyexiv2`. When a local filesystem path is available (`backend.local_path()` returns non-`None`), the file is opened directly with no temporary copy. For remote backends, the file is staged to a temporary file first.
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

Image processing (Rust binary + subprocess wrappers) lives in the separate [`ouestcharlie-imageproc`](https://github.com/ouestcharlie/outestcharlie-imageproc) package. See [imageproc_LLD.md](../outestcharlie-imageproc/imageproc_LLD.md) for the protocol specification and command reference.

This toolkit provides two higher-level builders that use `ouestcharlie-imageproc`:

**`thumbnail_builder.py`** — AVIF grid generation: photos are sorted by `content_hash`, split into chunks of ≤64 (8-column grid, up to 8 rows per AVIF file), and encoded in parallel; returns `list[ThumbnailChunk]`.

**`preview_builder.py`** — on-demand JPEG preview generation:
- `generate_preview_jpeg(image_proc, backend, partition, entry)` — generates and caches a single-photo JPEG preview; fast path if already cached

## Dependencies

| Dependency | Purpose | Version constraint |
|---|---|---|
| `mcp` | MCP server SDK | `>=1.0` |
| `lancedb` | Columnar photo index (schema v3+) | `>=0.20` |
| `pyarrow` | LanceDB schema definition and data conversion | `>=18` |
| `pyexiv2` | EXIF/XMP read-write (wraps Exiv2) | `>=2.8` |
| `ouestcharlie-imageproc` | Rust coprocessor for photo decode, resize, AVIF grid and JPEG preview | `>=1.0.0` |

## References

- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [MCP Specification (2025-11-25)](https://modelcontextprotocol.io/specification/2025-11-25)
- [pyexiv2](https://github.com/LeoHsiao1/pyexiv2) — EXIF/IPTC/XMP read-write
- [XMP Specification (ISO 16684)](https://www.iso.org/standard/75163.html)
- [HLD § Consistency Model](../../HLD.md) — optimistic concurrency design
