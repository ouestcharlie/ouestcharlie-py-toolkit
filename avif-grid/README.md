# avif-grid

Rust CLI that assembles JPEG tiles into an AVIF grid container.

## Build

Requires Rust (install via [rustup](https://rustup.rs)) and the system libavif:

```bash
brew install libavif    # macOS — provides pre-compiled libavif + libaom encoder
cargo build --release
```

No nasm, cmake, or meson required. The build links against the system libavif found via `pkg-config`; no codec is built from source.

The binary is at `target/release/avif-grid`.

## Usage

Reads JSON from stdin, writes result JSON to stdout:

```bash
echo '{
  "tiles": ["tile0.jpg", "tile1.jpg", ...],
  "quality": 55,
  "output": "thumbnails.avif"
}' | avif-grid
# stdout: {"cols":32,"rows":4,"tileSize":256}
```

## Grid layout

- `cols = ceil(sqrt(n))`, `rows = ceil(n / cols)` — square-ish grid
- Last row padded with black tiles if `n` is not a multiple of `cols`
- Tiles are expected to be pre-ordered by photo `content_hash` (ascending) for stable indices
- All tiles must have the same dimensions
