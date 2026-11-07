"""Per-photo data models: the index entry and the parsed XMP sidecar."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ouestcharlie_toolkit.model.fields import PHOTO_FIELDS, FieldDef, FieldType
from ouestcharlie_toolkit.model.version import SCHEMA_VERSION


@dataclass
class PhotoEntry:
    """Per-photo metadata entry within a leaf manifest.

    Searchable metadata (driven by PHOTO_FIELDS) is stored in ``searchable``
    keyed by ``FieldDef.entry_attr``.  Unknown XMP fields are preserved in
    ``_extra``.
    """

    filename: str
    content_hash: str
    searchable: dict[str, Any] = field(default_factory=dict)
    metadata_version: int = 1
    xmp_version_token: str = ""
    _extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_sidecar(
        cls,
        filename: str,
        sidecar: XmpSidecar,
        content_hash: str,
        xmp_version_token: str,
        field_config: list[FieldDef] | None = None,
    ) -> PhotoEntry:
        """Build a PhotoEntry from an XmpSidecar."""
        if field_config is None:
            field_config = PHOTO_FIELDS
        searchable: dict[str, Any] = {}
        for fdef in field_config:
            if fdef.sidecar_attr is not None:
                val = getattr(sidecar, fdef.sidecar_attr, None)
                if fdef.type is FieldType.STRING_COLLECTION and val is not None:
                    val = list(val)  # defensive copy
                searchable[fdef.entry_attr] = val
        return cls(
            filename=filename,
            content_hash=content_hash,
            metadata_version=sidecar.metadata_version,
            xmp_version_token=xmp_version_token,
            searchable=searchable,
        )


@dataclass
class XmpSidecar:
    """Parsed representation of an XMP sidecar file."""

    content_hash: str | None = None
    metadata_version: int = 1
    schema_version: int = SCHEMA_VERSION
    date_taken: datetime | None = None
    gps: tuple[float, float] | None = None
    camera_make: str | None = None
    camera_model: str | None = None
    orientation: int | None = None
    rating: int | None = None  # xmp:Rating (0=unrated, 1-5=stars, -1=rejected)
    width: int | None = None  # pixel width (exif:PixelXDimension / tiff:ImageWidth)
    height: int | None = None  # pixel height (exif:PixelYDimension / tiff:ImageLength)
    # User tags as "|"-separated paths (e.g. "Places|Europe|France"); flat tags are
    # one-level paths. Read from lr:hierarchicalSubject first, then dc:subject.
    tags: list[str] = field(default_factory=list)
    description: str | None = None  # dc:description (human-readable caption, any language)
    # Camera shoot settings
    iso_speed: int | None = None  # exif:ISOSpeedRatings
    aperture: float | None = None  # exif:FNumber (e.g. 2.8)
    exposure_time: float | None = None  # exif:ExposureTime in seconds (e.g. 0.004 = 1/250)
    focal_length: float | None = None  # exif:FocalLength in mm
    focal_length_35mm: int | None = None  # exif:FocalLengthIn35mmFilm
    lens_model: str | None = None  # aux:Lens / exifEX:LensModel / Exif.Photo.LensModel
    # Video fields. media_type defaults to "photo" so every existing photo sidecar
    # keeps its meaning without a rewrite; the video-only fields stay None for photos.
    media_type: str = "photo"  # "photo" | "video"
    duration_seconds: float | None = None  # container duration in seconds
    video_codec: str | None = None  # video stream codec name (e.g. "h264", "hevc")
    has_audio: bool | None = None  # whether the container carries an audio stream
    # Unknown XMP attributes and child elements from third-party apps (Lightroom, darktable, …).
    # Keys use Clark notation: "{ns_uri}localname".
    # Values are either plain strings (for simple attributes) or XML-serialized strings (for
    # structured child elements like bags/sequences, identifiable by a leading "<").
    _extra: dict[str, str] = field(default_factory=dict)
    # Tag paths matching the library's excluded tag prefixes (e.g. darktable|format|jpg).
    # Never indexed; written back to lr:hierarchicalSubject so they survive a round trip.
    _excluded_tags: list[str] = field(default_factory=list)
