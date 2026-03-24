# Python Toolkit LLD — Design Rationale

Rationale for non-obvious decisions in the toolkit implementation. For the design itself, see [py_toolkit_LLD.md](py_toolkit_LLD.md).

---

## Thumbnail pipeline: decode+resize+fit in Rust, not Python

### Decision

All image decoding, orientation correction, resizing, and square-fitting is performed inside the `image-proc` Rust binary rather than in Python. The Python side only stages photo bytes to a local temp directory, calls the binary once per tier, and writes the resulting AVIF to the backend.

### Why Rust for image processing

**Performance.** Python (Pillow) is single-threaded by design due to the GIL. Even with `asyncio.to_thread`, each photo occupies a thread for the full decode+resize+fit cycle. With `rayon::par_iter` in Rust, all photos in a partition are decoded in parallel across CPU cores with zero GIL contention. For 10 K photos this is the dominant operation.

**Simpler async boundary.** The original Python pipeline called `asyncio.to_thread` three times per photo (decode, fit, encode), each returning to the event loop only to immediately dispatch another CPU-bound call. The Rust binary removes this churn entirely: one `asyncio.create_subprocess_exec` call per tier, regardless of partition size.

**Fewer Python optional dependencies.** `rawpy` (wraps LibRaw, compiled C extension) and `pillow-heif` (wraps libheif) are non-trivial to install and platform-specific. Moving format dispatch to Rust means the Python toolkit has no image-decoding dependencies at all. Format support is a compile-time concern of the binary.

### RAW and HEIC as compile-time features

RAW decoding (`rawler`, pure Rust) and HEIC decoding (`libheif-rs`, requires system libheif) are Cargo features rather than hard dependencies. This keeps the default binary lean and avoids forcing `brew install libheif` on all developers. The binary returns a clear error message if a format is not compiled in, making the failure mode obvious.

`rawler` is pre-1.0 (API unstable); the version is pinned exactly (`=0.7.2`) to avoid surprise breakage.

