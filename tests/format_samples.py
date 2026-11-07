"""Small sample files, one per photo format family, for the format coverage tests.

Formats Pillow can write are generated on the fly with known EXIF. JPEG XL and
RAW are committed under sample-images/formats/ (see make_jxl_fixtures.py).

Every generated image is a 64x48 landscape gradient. The EXIF orientation is 6
(rotate 90° CW) where the format carries it, so tests can check that thumbnails
and previews come out upright (portrait).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

SAMPLES_DIR = Path(__file__).parent / "sample-images"
FORMATS_DIR = SAMPLES_DIR / "formats"

MAKE = "TestMake"
MODEL = "TestModel"
DATE = "2024:05:01 12:34:56"
W, H = 64, 48


@dataclass(frozen=True)
class Sample:
    """Expected metadata of a sample file."""

    filename: str
    make: str | None
    date: str | None  # ISO, as XmpSidecar.date_taken.isoformat()
    orientation: int | None
    width: int  # as stored in the sidecar
    height: int
    display: tuple[int, int]  # upright (width, height) of the decoded photo


def _gradient() -> Image.Image:
    img = Image.new("RGB", (W, H))
    img.putdata([(x * 4, y * 5, 128) for y in range(H) for x in range(W)])
    return img


def _exif() -> Image.Exif:
    exif = Image.Exif()
    exif[271] = MAKE
    exif[272] = MODEL
    exif[274] = 6
    exif.get_ifd(0x8769)[36867] = DATE
    return exif


_ISO_DATE = "2024-05-01T12:34:56"
_STORED = {"make": MAKE, "date": _ISO_DATE, "orientation": 6, "width": W, "height": H}

SAMPLES: dict[str, Sample] = {
    # Stored-orientation convention: EXIF orientation verbatim, stored dimensions.
    "jpeg": Sample("photo.jpg", **_STORED, display=(H, W)),
    "png": Sample("photo.png", **_STORED, display=(H, W)),
    "tiff": Sample("photo.tif", **_STORED, display=(H, W)),
    "webp": Sample("photo.webp", **_STORED, display=(H, W)),
    # No EXIF in these formats.
    "gif": Sample("photo.gif", None, None, None, W, H, display=(W, H)),
    "bmp": Sample("photo.bmp", None, None, None, W, H, display=(W, H)),
    # Upright convention: the decoder applies the file's own rotation (AVIF irot
    # box, written by Pillow from the EXIF orientation), so orientation 1 and
    # display dimensions.
    "avif": Sample("photo.avif", MAKE, _ISO_DATE, 1, H, W, display=(H, W)),
    # The EXIF orientation (6) disagrees with the codestream (1): the codestream wins.
    "jxl": Sample("jxl_exif.jxl", MAKE, _ISO_DATE, 1, W, H, display=(W, H)),
    "jxl-bare": Sample("jxl_bare.jxl", None, None, 1, W, H, display=(W, H)),
    # RAW: Sigma fp DNG from raw.pixls.us (CC0). The raw image is 1936x1090;
    # its DefaultCropSize, the developed size, is 1920x1080.
    "dng": Sample(
        "sigma_fp.dng", "SIGMA", "2024-02-29T19:10:21-04:00", 1, 1920, 1080, display=(1920, 1080)
    ),
}


def write_sample(directory: Path, key: str) -> Path:
    """Write the sample for *key* into *directory* and return its path."""
    sample = SAMPLES[key]
    path = directory / sample.filename
    committed = FORMATS_DIR / sample.filename
    if committed.exists():
        shutil.copyfile(committed, path)
    elif key in ("gif", "bmp"):
        _gradient().save(path)
    else:
        _gradient().save(path, exif=_exif().tobytes())
    return path
