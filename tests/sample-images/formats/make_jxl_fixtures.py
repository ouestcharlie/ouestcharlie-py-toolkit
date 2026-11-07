"""Generate the JPEG XL fixtures used by test_photo.py.

Pillow cannot write JPEG XL, so these two small files are committed:

- jxl_bare.jxl: bare codestream, no container and no EXIF (cjxl's default output).
- jxl_exif.jxl: ISO BMFF container with an EXIF box. Its EXIF orientation is 6,
  but the codestream orientation is 1: decoders follow the codestream, so the
  photo displays as stored (64x48).

The encoders are not dependencies of this package; run with:

    uv run --with imagecodecs --with numpy \\
        python tests/sample-images/formats/make_jxl_fixtures.py

Needs `exiftool` on PATH.
"""

from __future__ import annotations

import pathlib
import subprocess

import imagecodecs
import numpy as np

OUT = pathlib.Path(__file__).parent
W, H = 64, 48


def gradient() -> np.ndarray:
    """RGB gradient: red grows left to right, green top to bottom."""
    r = np.tile(np.linspace(0, 255, W), (H, 1))
    g = np.tile(np.linspace(0, 255, H)[:, None], (1, W))
    b = np.full((H, W), 128)
    return np.stack([r, g, b], axis=-1).round().astype(np.uint8)


def main() -> None:
    pixels = gradient()
    (OUT / "jxl_bare.jxl").write_bytes(imagecodecs.jpegxl_encode(pixels, lossless=True))

    exif_path = OUT / "jxl_exif.jxl"
    exif_path.write_bytes(imagecodecs.jpegxl_encode(pixels, lossless=True))
    subprocess.run(
        [
            "exiftool",
            "-q",
            "-m",  # wrapping the codestream in a container is reported as a warning
            "-overwrite_original",
            "-Make=TestMake",
            "-Model=TestModel",
            "-Orientation#=6",
            "-DateTimeOriginal=2024:05:01 12:34:56",
            str(exif_path),
        ],
        check=True,
    )


if __name__ == "__main__":
    main()
