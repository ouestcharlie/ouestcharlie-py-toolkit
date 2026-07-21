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

V1 scope: local filesystem backend only. The `backend.py` abstraction enables adding cloud backends (S3, GCS, ADLS Gen2) later without changing agent code.

## Backend Abstraction

`VersionToken` is opaque to callers — `mtime` for local, `ETag` or `generation` for future remote backends.

`content_hash` is BLAKE3 truncated to 128 bits, base64url-encoded (22 chars). Future remote backends can override to fetch the provider checksum without downloading the file.

**Cross-process locking**: `write_conditional` holds two locks simultaneously for the stat-check + rename — a per-path `threading.Lock` (required on macOS/BSD where `flock` is per-process and does not serialize threads within the same process) and a `_CrossProcessLock` on a `<filename>.lock` sidecar file. Lock files are always created inside `.ouestcharlie/`, never next to original photos.

**Cloud-Mounted Backend**: overrides `read()` with an exponential-backoff retry loop — Windows CF API may return partial data for dehydrated placeholder files.

## LanceDB Columnar Index (`lance_index.py`)

The LanceDB index replaces per-partition `manifest.json` files as the primary per-photo metadata store (schema version 3+). It is a single columnar table containing one row per photo across all partitions.

### Index location

By default the index lives at `.ouestcharlie/index.lance/` within the backend root. On Windows, when the library root is a UNC path or a mapped drive that resolves to one, `object_store` (the Rust storage layer under Lance) cannot reliably open files on the network share. The index is then redirected to a local NTFS path (`%LOCALAPPDATA%\ouestcharlie\indexes\<library_name>\`) set by Woof and propagated via `WOOF_BACKEND_CONFIG`. LanceDB accepts local filesystem paths directly — no `file://` URI conversion.

All operations use the `AsyncTable` API. `merge_insert().execute()` returns a coroutine and is awaited directly, not run in a thread.

### Schema non-obvious choices

GPS and thumbnail columns use **flat nullable columns** rather than structs. LanceDB returns null struct fields as default-valued dicts, not `None`, which would require extra handling at every read site.

`date_taken` is stored timezone-naive. All comparisons must strip timezone before comparing.

### `search_where` pagination

Two queries are issued: a lightweight `select(["tags"])` scan for total count and tag facets, then a page query with `order_by` / `offset` / `limit` pushed down to LanceDB (available since 0.33). A `filename` tiebreaker is appended to the ordering for deterministic pagination across pages.

### Partition summary

DuckDB runs in `asyncio.to_thread` because it is CPU-bound sync. The Lance query before it uses native async — no `to_thread` wrapping needed there.

## Manifest Consistency

Unknown fields in `summary.json` are captured in `_extra: dict` and round-tripped through serialization, following HLD schema evolution rules. Agents must never drop fields they don't recognize.

## XMP Consistency

**Conflict-free merges**: agents write non-overlapping field sets (face tags, scene tags, EXIF fields). Most retry scenarios in the optimistic concurrency loop are therefore trivial merges with no actual conflict.

**Third-party sidecar preservation**: `XmpSidecar` holds `_raw_xml` so fields written by Lightroom, darktable, or ExifTool survive round-trips through OuEstCharlie.

**Dual sidecar naming convention**: `xmp_path_for(photo_path, with_photo_extension=bool)` supports both the full-extension form (`IMG_001.cr3.xmp` — darktable, digiKam, Immich's preferred form) and the extension-stripped form (`IMG_001.xmp` — Lightroom). `XmpStore.read()`/`write()` resolve to whichever form already exists on disk (full-extension checked first), and `create()` always writes new sidecars in the full-extension form. This means existing libraries need no migration, and an update to an existing sidecar never forks a second file under the other convention.

**Namespace registration**: Python 3.13 `ET.register_namespace()` rejects prefixes matching `ns\d+` — use `ext{counter}` as fallback.

**`XPKeywords` tag bootstrap**: `Photo.extract_exif()` seeds `tags` from the Windows-specific `Exif.Image.XPKeywords` EXIF field (semicolon-separated) when creating a brand-new sidecar. This is a one-time bootstrap for libraries whose only keyword source is Windows Explorer/Photos tagging — it is not an authoritative or bidirectional sync with `dc:subject`, since `extract_exif()` never reads an existing sidecar and therefore never overwrites tags added later via Woof or Darktable.

## References

- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [MCP Specification (2025-11-25)](https://modelcontextprotocol.io/specification/2025-11-25)
- [pyexiv2](https://github.com/LeoHsiao1/pyexiv2) — EXIF/IPTC/XMP read-write
- [XMP Specification (ISO 16684)](https://www.iso.org/standard/75163.html)
- [HLD § Consistency Model](../../HLD.md) — optimistic concurrency design
- [image-proc LLD](../ouestcharlie-imageproc/imageproc_LLD.md) — Rust coprocessor protocol
