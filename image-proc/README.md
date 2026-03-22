# image-proc

Rust CLI that decodes photos, resizes and fits them to square tiles, and assembles them into an AVIF grid container. Also generates JPEG previews for individual photos.

## Build

Requires Rust (install via [rustup](https://rustup.rs)) and the system libavif:

```bash
brew install libavif    # macOS — provides pre-compiled libavif + libaom encoder
# apt install libavif-dev  # Linux

cargo build --release
```

No nasm, cmake, or meson required. The build links against the system libavif found via `pkg-config`; no codec is built from source.

The binary is at `target/release/image-proc`.

> **Note:** When installing `ouestcharlie-toolkit` from PyPI, the binary is compiled automatically by the hatch build hook and bundled inside the wheel — no manual build step needed.

### Optional features

```bash
cargo build --release --features raw   # RAW format support (rawler, pure Rust)
cargo build --release --features heic  # HEIC/HEIF support (requires libheif)
```

## Usage

Reads JSON from stdin, writes result JSON to stdout:

```bash
echo '{
  "photos": [
    { "path": "/tmp/photo0.jpg", "ext": ".jpg", "orientation": 1, "content_hash": "sha256:aaa..." },
    { "path": "/tmp/photo1.cr2", "ext": ".cr2", "orientation": 6, "content_hash": "sha256:bbb..." }
  ],
  "tile_size": 256,
  "fit": "crop",
  "quality": 55,
  "output": "/tmp/thumbnails.avif"
}' | image-proc
# stdout: {"cols":2,"rows":1,"tileSize":256,"photoOrder":["sha256:aaa...","sha256:bbb..."]}
```

### Parameters

| Field | Type | Description |
|-------|------|-------------|
| `photos` | array | Photos to include, in tile order (sort by `content_hash` for stable indices) |
| `photos[].path` | string | Absolute path to the staged photo file |
| `photos[].ext` | string | File extension including dot (e.g. `".jpg"`, `".cr2"`) |
| `photos[].orientation` | int or null | TIFF orientation value 1–8; `null` means no transform |
| `photos[].content_hash` | string | `sha256:<hex>` — echoed back in `photoOrder` |
| `tile_size` | int | Square tile side in pixels |
| `fit` | string | `"crop"` (center-crop) or `"pad"` (letterbox with black) |
| `quality` | int | AVIF quality 0–100 (higher = better) |
| `output` | string | Absolute path for the output AVIF file |

### Output

| Field | Description |
|-------|-------------|
| `cols`, `rows` | Grid dimensions |
| `tileSize` | Tile side in pixels (echoes `tile_size` input) |
| `photoOrder` | `content_hash` values in tile order (same order as input `photos`) |

## Pipeline

For each photo (in parallel via rayon):
1. Decode — JPEG/PNG/WebP/TIFF via `image` crate; RAW via `rawler` (`--features raw`); HEIC via `libheif-rs` (`--features heic`)
2. Apply TIFF orientation (values 1–8)
3. Resize:
   - `"crop"` mode: resize so short edge == `tile_size`
   - `"pad"` mode: resize so long edge == `tile_size`
4. Fit to square:
   - `"crop"`: center-crop to `tile_size × tile_size`
   - `"pad"`: paste centered on a black `tile_size × tile_size` canvas

Then, sequentially (libavif is not thread-safe):
5. Convert each RGB tile to YUV420 via libavif
6. Encode grid to AVIF with `avifEncoderAddImageGrid`
7. Write output file

## Grid layout

- `cols = ceil(sqrt(n))`, `rows = ceil(n / cols)` — square-ish grid
- Last row padded with black tiles when `n` is not a multiple of `cols`
- Caller is responsible for sorting photos by `content_hash` before passing them, to ensure stable tile indices across renames and additions
