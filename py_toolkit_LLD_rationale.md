# Python Toolkit LLD — Design Rationale

Rationale for non-obvious decisions in the toolkit implementation. For the design itself, see [py_toolkit_LLD.md](py_toolkit_LLD.md).

---

## Sub-packages instead of a flat package (OEC-72)

### Decision

The toolkit is split into seven sub-packages with a fixed layer order (see the LLD, "Package Layout"), checked by a test rather than by an extra tool such as import-linter.

### Why

**Files had grown to several concerns each.** `xmp.py` (~940 lines) mixed paths, namespaces, value parsing, the store, parsing and serialization; `photo.py`, `video.py`, `lance_index.py` and `schema.py` were similar. Splitting by responsibility brings most files under ~300 lines (the largest, `storage/local.py`, was left as is) and makes the pure helpers testable on their own.

**Agents depended on private names.** Wally imported `_esc` and `_summary_to_dict`. Sub-package `__init__` files now define what agents may import; anything else can change without notice.

**Absolute imports.** With nested sub-packages, relative imports turn into `..`/`...` chains that change every time a module moves. Absolute imports also let one codemod rewrite toolkit and agents the same way.

**`renditions` isolated.** It is the only code that needs the native `image-proc` binary. Keeping it apart leaves the option of making `ouestcharlie-imageproc` an optional extra for read-only agents.

**No compatibility shims.** All consumers (Wally, Whitebeard) are released with the toolkit in the same cycle, so the old flat module paths were removed rather than kept as re-exports.

---

## Thumbnail pipeline: decode+resize+fit in Rust, not Python

### Decision

All image decoding, orientation correction, resizing, and square-fitting is performed inside the `image-proc` Rust binary rather than in Python. The Python side only stages photo bytes to a local temp directory, calls the binary once per tier, and writes the resulting AVIF to the backend.

### Why Rust for image processing

**Performance.** Python (Pillow) is single-threaded by design due to the GIL. Even with `asyncio.to_thread`, each photo occupies a thread for the full decode+resize+fit cycle. With `rayon::par_iter` in Rust, all photos in a partition are decoded in parallel across CPU cores with zero GIL contention. For 10 K photos this is the dominant operation.

**Simpler async boundary.** The original Python pipeline called `asyncio.to_thread` three times per photo (decode, fit, encode), each returning to the event loop only to immediately dispatch another CPU-bound call. The Rust binary removes this churn entirely: one `asyncio.create_subprocess_exec` call per tier, regardless of partition size.

**Fewer Python optional dependencies.** `rawpy` (wraps LibRaw, compiled C extension) and `pillow-heif` (wraps libheif) are non-trivial to install and platform-specific. Moving format dispatch to Rust means the Python toolkit has no image-decoding dependencies at all. Format support is a compile-time concern of the binary.

### RAW and HEIC as compile-time features

RAW decoding (`rawler`, pure Rust) and HEIC decoding (`libheif-rs`, requires system libheif) are Cargo features rather than hard dependencies. This keeps the default binary lean and avoids forcing `brew install libheif` on all developers building from source. The binary returns a clear error message if a format is not compiled in, making the failure mode obvious.

Published wheels enable both features via CI regardless (`IMAGE_PROC_FEATURE_RAW=1 IMAGE_PROC_FEATURE_HEIC=1`), so end users installing from PyPI get HEIC support out of the box — the runtime system-dependency concern is solved by bundling `libheif`'s shared library into the wheel at build time (via `auditwheel`/`delocate`/DLL co-location, one per platform), not by leaving the feature uncompiled. `libheif` is a build-time-only dependency from the end user's perspective, same as `nasm`.

`rawler` is pre-1.0 (API unstable); the version is pinned exactly (`=0.7.2`) to avoid surprise breakage.

