# Development Guide

## Setup

### System dependencies

`pyexiv2` links against `libexiv2` which requires `inih` at runtime on macOS:

```bash
brew install inih    # macOS only
```

`image-proc` (Rust) uses [ravif](https://crates.io/crates/ravif) for AVIF encoding — pure Rust, no system libavif needed. However, rav1e's assembly optimisations require **nasm** at compile time:

```bash
brew install nasm          # macOS
sudo apt install nasm      # Linux
choco install nasm         # Windows
```

No other system libraries are required for building image-proc.

### Create virtual environment and install dependencies

```bash
uv venv --python 3.13
uv pip install -e ".[dev]"
```

### Build the image-proc binary

The binary is **not** compiled automatically in editable installs. Build it once:

```bash
cd image-proc
cargo build --release
# binary: image-proc/target/release/image-proc
```

With optional features:

```bash
cargo build --release --features raw    # RAW format support (pure Rust, no extra deps)
cargo build --release --features heic   # HEIC support (requires brew install libheif)
```

## Running Tests

**Always use `.venv/bin/python -m pytest`** — do not use `.venv/bin/pytest` or a system `python`:

```bash
# Run all tests
.venv/bin/python -m pytest tests/ -v

# Run a specific file
.venv/bin/python -m pytest tests/test_photo.py -v --tb=short
```

> Why: `pytest` on PATH or `uv run pytest` may resolve to the wrong Python or fail on native deps (e.g. rawpy has no macOS x86_64 wheel).

## Building a Wheel

The `hatch_build.py` hook compiles `image-proc` and bundles the binary inside the wheel:

```bash
pip install hatch
hatch build
# produces dist/ouestcharlie_toolkit-*.whl (platform-specific)
```

Set env vars to enable optional features:

```bash
IMAGE_PROC_FEATURE_RAW=1 hatch build
IMAGE_PROC_FEATURE_HEIC=1 hatch build
```

## Project Structure

```
ouestcharlie-py-toolkit/
├── hatch_build.py            # Build hook: compiles image-proc, bundles binary in wheel
├── image-proc/               # Rust CLI source
│   ├── Cargo.toml
│   └── src/main.rs
├── src/
│   └── ouestcharlie_toolkit/ # Python package
│       └── bin/              # Bundled binary (populated at build time, gitignored)
├── tests/
├── pyproject.toml
├── README.md                 # Usage and PyPI install docs
└── README_DEV.md             # This file
```
