# ouestcharlie-py-toolkit — Claude Working Rules

## Running Tests

**Always use the project's own `.venv`:**

```
.venv/bin/python -m pytest tests/ -v
```

## Key Design Patterns

- **Python 3.13 ET restriction**: `ET.register_namespace()` rejects prefixes matching `ns\d+` — use `ext{counter}` as fallback.

## image-proc Version Bumping

When changing the JSON protocol between Python and the image-proc Rust binary (new request fields, new response fields, new commands, or changed behavior), **bump the minor version in both**:

1. `image-proc/Cargo.toml` → `version = "X.Y.Z"`
2. `pyproject.toml` → `[tool.ouestcharlie] image_proc_min_version = "X.Y.Z"`

Both values must be kept in sync. The Python toolkit reads `image_proc_min_version` from `pyproject.toml` at runtime and rejects any binary older than that version.

## pyexiv2 Notes

- Version: 2.x (LeoHsiao1 binding). `convert_exif_to_xmp()` does **not** exist — use `read_exif()`.
- `read_exif()` returns `dict[str, str]` with pyexiv2 key format: `Exif.Image.Make`, `Exif.Photo.DateTimeOriginal`, etc.
- GPS values are DMS rational strings: `"48/1 52/1 1234/100"` → parse with `_exif_rational_to_float()`.