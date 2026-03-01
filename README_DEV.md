# Development Guide

## Setup

### System dependencies (macOS)

`pyexiv2` links against `libexiv2` which requires `inih` on macOS:

```bash
brew install inih
```

This is only needed for `extract_exif` (EXIF extraction from image files). `parse_xmp` and `serialize_xmp` have no native dependencies. On Linux, `pyexiv2` wheels bundle their dependencies.

### Create virtual environment and install dependencies

```bash
# Create virtual environment with Python 3.13 (required for package compatibility)
uv venv --python 3.13

# Install package in editable mode with dev dependencies
uv pip install -e ".[dev]"
```

### Activate virtual environment

```bash
# macOS/Linux
source .venv/bin/activate

# When active, you can use python/pytest directly:
python tests/test_structure.py
pytest tests/
```

## Running Tests

```bash
# Run all tests
.venv/bin/pytest tests/

# Run with verbose output
.venv/bin/pytest -v tests/

# Run specific test file
.venv/bin/pytest tests/test_schema.py

# Run with coverage (if coverage is installed)
.venv/bin/pytest --cov=ouestcharlie_toolkit tests/
```

## Project Structure

```
ouestcharlie-py-toolkit/
├── src/
│   └── ouestcharlie/         # Main package
├── tests/                    # Test directory
├── .venv/                    # Virtual environment (gitignored)
├── pyproject.toml            # Package configuration
└── README.md                 # Usage documentation
```
