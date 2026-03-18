# Python Toolkit LLD — Design Rationale

Rationale for non-obvious decisions in the toolkit implementation. For the design itself, see [py_toolkit_LLD.md](py_toolkit_LLD.md).

---

## Thumbnail pipeline: decode+resize+fit in Rust, not Python

### Decision

All image decoding, orientation correction, resizing, and square-fitting is performed inside the `avif-grid` Rust binary rather than in Python. The Python side only stages photo bytes to a local temp directory, calls the binary once per tier, and writes the resulting AVIF to the backend.

### Why Rust for image processing

**Performance.** Python (Pillow) is single-threaded by design due to the GIL. Even with `asyncio.to_thread`, each photo occupies a thread for the full decode+resize+fit cycle. With `rayon::par_iter` in Rust, all photos in a partition are decoded in parallel across CPU cores with zero GIL contention. For 10 K photos this is the dominant operation.

**Simpler async boundary.** The original Python pipeline called `asyncio.to_thread` three times per photo (decode, fit, encode), each returning to the event loop only to immediately dispatch another CPU-bound call. The Rust binary removes this churn entirely: one `asyncio.create_subprocess_exec` call per tier, regardless of partition size.

**Fewer Python optional dependencies.** `rawpy` (wraps LibRaw, compiled C extension) and `pillow-heif` (wraps libheif) are non-trivial to install and platform-specific. Moving format dispatch to Rust means the Python toolkit has no image-decoding dependencies at all. Format support is a compile-time concern of the binary.

### Why drop the JPEG tile cache

The original design cached each photo's decoded+fitted JPEG tile on the backend so that regenerating the AVIF (e.g. after a quality change) did not require re-decoding. The cache was invalidated by `content_hash`, so renamed photos did not cause re-decoding.

With Rust+rayon, re-decoding all photos in a partition is fast enough that the cache's amortisation benefit does not justify its cost:
- Extra backend reads/writes (one per photo per tier)
- Cache invalidation complexity (stale tiles after format changes)
- Two-stage pipeline (ensure_tile → assemble_avif) instead of one

The cache can be reintroduced if profiling shows decode time is dominant at scale.

### RAW and HEIC as compile-time features

RAW decoding (`rawler`, pure Rust) and HEIC decoding (`libheif-rs`, requires system libheif) are Cargo features rather than hard dependencies. This keeps the default binary lean and avoids forcing `brew install libheif` on all developers. The binary returns a clear error message if a format is not compiled in, making the failure mode obvious.

`rawler` is pre-1.0 (API unstable); the version is pinned exactly (`=0.7.2`) to avoid surprise breakage.

---

## `decode_and_resize` API: bytes-only, no file paths

### Decision

`decode_and_resize` (now removed; previously in `thumbnail.py`) accepted `bytes` + `ext` rather than a file path string.

### Why

The backend abstraction is storage-agnostic: photos may live on a local filesystem, S3, GCS, or a mounted cloud drive. The only operation guaranteed by the `Backend` protocol is `read() → bytes`. Accepting file paths would require the caller to know that the bytes are already on a local disk, breaking the abstraction.

For RAW formats, `rawpy` required a file path (no in-memory API). The solution was to write a temporary file *inside* `decode_and_resize` for RAW, hidden from the caller. This kept the call site clean and the temp file short-lived.

This design is now moot — the function is removed and the Rust binary handles it — but the same principle (bytes in, not paths) applies to the staging step in `_call_avif_grid`.

---

## `asyncio.to_thread` consolidation

### Decision

The three sequential `asyncio.to_thread` calls (decode, fit, encode) were collapsed into a single call wrapping a synchronous helper function.

### Why

Each `to_thread` call schedules a thread-pool task, suspends the coroutine, and resumes it on completion — even when the next operation is immediately another blocking call with no I/O in between. Three calls where one suffices adds unnecessary scheduler overhead and makes the control flow harder to follow. The fix was a local `_decode_fit_encode()` closure that runs all three steps synchronously, dispatched once.

This is now also moot (Rust handles it), but the principle — avoid `to_thread` hops between tightly coupled CPU-bound steps — remains relevant elsewhere.

---

## BytesIO vs temp file at the output boundary

### Decision

JPEG tile encoding used `io.BytesIO` as the destination for `PIL.Image.save()`, not a temp file.

### Why

Pillow's `save()` requires a writable file-like object or a path. `BytesIO` gives in-memory bytes with no disk I/O. Writing to a temp file and reading it back would be strictly slower and add cleanup complexity. The bytes from `buf.getvalue()` were passed directly to `backend.write_new()`.

Again, now moot, but the principle — use `BytesIO` for in-process encode-then-write — is correct.
