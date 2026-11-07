"""File extensions handled by the pipeline (lowercase, with the dot)."""

# RAW extensions decoded by image-proc (rawler). Formats rawler cannot decode
# reliably are left out on purpose (imageproc_LLD.md, "RAW decoding").
RAW_SUFFIXES: frozenset[str] = frozenset(
    {
        # Current camera makers
        ".cr2",
        ".cr3",
        ".crw",
        ".nef",
        ".nrw",
        ".arw",
        ".srf",
        ".sr2",
        ".dng",
        ".raf",
        ".orf",
        ".rw2",
        ".rwl",
        ".pef",
        ".srw",
        ".erf",
        # Medium format
        ".3fr",
        ".fff",
        ".iiq",
        ".mos",
    }
)

# Photo extensions handled by the pipeline (lowercase, with the dot): the files
# Whitebeard indexes. Each one is decoded by image-proc, see its
# supported_extensions().
PHOTO_SUFFIXES: frozenset[str] = (
    frozenset(
        {
            # Standard formats
            ".jpg",
            ".jpeg",
            ".png",
            ".tif",
            ".tiff",
            ".webp",
            ".gif",
            ".bmp",
            ".jxl",
            # HEIF container (HEVC or AV1)
            ".heic",
            ".heif",
            ".hif",
            ".avif",
        }
    )
    | RAW_SUFFIXES
)

# pyexiv2's bundled libexiv2 lacks libheif support; these suffixes are read
# via pillow-heif + Pillow instead (see read_heif_exif).
HEIF_SUFFIXES: frozenset[str] = frozenset({".heic", ".heif", ".hif"})


# Container suffixes handled as video (V1: QuickTime MOV + MP4). Used the same
# caller-driven way as HEIF_SUFFIXES / Backend.list_files(suffixes=).
VIDEO_SUFFIXES: frozenset[str] = frozenset({".mov", ".mp4"})
