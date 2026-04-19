# ouestcharlie-py-toolkit — Claude Working Rules

## Running Tests

See [README.md](../README.md#running-tests) for the full command reference. Quick summary:

```bash
# Unit tests
.venv/bin/python -m pytest tests/ -v

# Integration tests (require image-proc binary)
.venv/bin/python -m pytest tests_integration/ -v

# Rust tests
cd image-proc && cargo test
```

## Key Design Patterns

- **Python 3.13 ET restriction**: `ET.register_namespace()` rejects prefixes matching `ns\d+` — use `ext{counter}` as fallback.

## image-proc Version Bumping

When changing the JSON protocol between Python and the image-proc Rust binary (new request fields, new response fields, new commands, or changed behavior), **bump both**:

1. `image-proc/Cargo.toml` → `version = "X.Y.Z"` (bump minor for compatible additions, major for breaking changes)
2. `src/ouestcharlie_toolkit/image_proc.py` → `IMAGE_PROC_PROTOCOL_MAJOR_VERSION = X`

The major component must match: image-proc validates `protocol_version` in every incoming JSON request and returns an in-band error if the major differs. No subprocess overhead — no `--version` call needed.

## Code Style

**SIM117 — always group `with` / `async with` statements** (ruff enforced):

```python
# bad
with foo() as a:
    with bar() as b:
        ...

# good
with (
    foo() as a,
    bar() as b,
):
    ...
```

This applies to `with`, `async with`, and `patch()` context managers in tests.

## pyexiv2 Notes

- Version: 2.x (LeoHsiao1 binding). `convert_exif_to_xmp()` does **not** exist — use `read_exif()`.
- `read_exif()` returns `dict[str, str]` with pyexiv2 key format: `Exif.Image.Make`, `Exif.Photo.DateTimeOriginal`, etc.
- GPS values are DMS rational strings: `"48/1 52/1 1234/100"` → parse with `_exif_rational_to_float()`.