# ouestcharlie-py-toolkit — Claude Working Rules

## Running Tests

**Always use the project's own `.venv`:**

```
.venv/bin/python -m pytest tests/ -v
```

## Key Design Patterns

- **Python 3.13 ET restriction**: `ET.register_namespace()` rejects prefixes matching `ns\d+` — use `ext{counter}` as fallback.

## pyexiv2 Notes

- Version: 2.x (LeoHsiao1 binding). `convert_exif_to_xmp()` does **not** exist — use `read_exif()`.
- `read_exif()` returns `dict[str, str]` with pyexiv2 key format: `Exif.Image.Make`, `Exif.Photo.DateTimeOriginal`, etc.
- GPS values are DMS rational strings: `"48/1 52/1 1234/100"` → parse with `_exif_rational_to_float()`.