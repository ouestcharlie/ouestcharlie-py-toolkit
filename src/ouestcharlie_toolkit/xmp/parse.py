"""Parse XMP XML into an XmpSidecar."""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from collections.abc import Iterable

from ouestcharlie_toolkit.model.entries import XmpSidecar
from ouestcharlie_toolkit.model.tags import (
    DEFAULT_EXCLUDED_TAG_PREFIXES,
    is_excluded,
    leaf_paths,
    levels,
    normalize_path,
    normalize_paths,
)
from ouestcharlie_toolkit.model.version import SCHEMA_VERSION
from ouestcharlie_toolkit.xmp.namespaces import (
    KNOWN_ATTRS,
    KNOWN_CHILDREN,
    NS_AUX,
    NS_DC,
    NS_EXIF,
    NS_EXIFEX,
    NS_LR,
    NS_OC,
    NS_RDF,
    NS_TIFF,
    NS_XMP,
    NS_XMP_LEGACY,
    find_description,
    register_et_namespaces,
)
from ouestcharlie_toolkit.xmp.values import parse_iso_datetime, parse_xmp_gps

_log = logging.getLogger(__name__)


def _strip_xpacket(xml: str) -> str:
    """Remove <?xpacket ...?> processing instructions before ET parsing."""
    lines = [line for line in xml.splitlines() if not line.strip().startswith("<?xpacket")]
    return "\n".join(lines).strip()


def _get_simple(desc: ET.Element, key: str) -> str | None:
    """Return a simple property, written as an attribute or as a child element."""
    value = desc.get(key)
    if value is not None:
        return value
    child = desc.find(key)
    if child is None or child.text is None:
        return None
    return child.text.strip()


def _read_li_list(elem: ET.Element | None) -> list[str] | None:
    """Return the rdf:li texts of an rdf:Bag/rdf:Seq child, or None when *elem* is absent."""
    if elem is None:
        return None
    rdf = f"{{{NS_RDF}}}"
    container = elem.find(f"{rdf}Bag")
    if container is None:
        container = elem.find(f"{rdf}Seq")
    if container is None:
        return []
    return [li.text or "" for li in container.findall(f"{rdf}li")]


# ---------------------------------------------------------------------------
# Tag helpers
# ---------------------------------------------------------------------------


def split_excluded(paths: list[str], prefixes: Iterable[str]) -> tuple[list[str], list[str]]:
    """Split tag paths into (user tags, excluded tags)."""
    prefixes = tuple(prefixes)
    kept = [p for p in paths if not is_excluded(p, prefixes)]
    excluded = [p for p in paths if is_excluded(p, prefixes)]
    return kept, excluded


def _read_tags(
    hierarchical: list[str] | None,
    subject: list[str] | None,
    excluded_tag_prefixes: Iterable[str],
) -> tuple[list[str], list[str]]:
    """Resolve user tag paths from lr:hierarchicalSubject and dc:subject.

    ``lr:hierarchicalSubject`` takes precedence, as in darktable. A ``dc:subject``
    term that is not a level of any hierarchical path was added by a tool that
    only writes ``dc:subject``: it is kept as a one-level path. Without
    ``lr:hierarchicalSubject``, each ``dc:subject`` term is read as a path.

    Returns:
        ``(tags, excluded)``: user tag paths (leaf paths only) and the paths
        matching *excluded_tag_prefixes*.
    """
    if hierarchical is not None:
        paths = normalize_paths(hierarchical)
        known_levels = {lvl for p in paths for lvl in levels(p)}
        flat_only = [
            t for t in (normalize_path(s) for s in subject or []) if t and t not in known_levels
        ]
        paths = normalize_paths(paths + flat_only)
    else:
        paths = normalize_paths(subject or [])
    tags, excluded = split_excluded(paths, excluded_tag_prefixes)
    return leaf_paths(tags), excluded


def parse_xmp(
    xml: str,
    excluded_tag_prefixes: Iterable[str] = DEFAULT_EXCLUDED_TAG_PREFIXES,
) -> XmpSidecar:
    """Parse XMP XML into an XmpSidecar.

    Known fields are mapped to typed XmpSidecar attributes. Unknown attributes
    and child elements on rdf:Description are stored in _extra for round-trip
    preservation (same pattern as manifest _extra dicts).

    Args:
        xml: XMP XML string (with or without <?xpacket ?> wrappers).
        excluded_tag_prefixes: Tag paths equal to or below one of these prefixes
            are kept out of ``tags`` and stored in ``_excluded_tags`` instead.

    Returns:
        XmpSidecar populated from the XMP data.
    """
    register_et_namespaces()
    body = _strip_xpacket(xml)

    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        _log.warning("Malformed XMP document, returning empty sidecar", exc_info=True)
        return XmpSidecar()

    desc = find_description(root)
    if desc is None:
        return XmpSidecar()

    oc = f"{{{NS_OC}}}"
    exif = f"{{{NS_EXIF}}}"
    tiff = f"{{{NS_TIFF}}}"
    xmp_ns = f"{{{NS_XMP}}}"
    dc = f"{{{NS_DC}}}"
    rdf = f"{{{NS_RDF}}}"
    aux = f"{{{NS_AUX}}}"
    exifex = f"{{{NS_EXIFEX}}}"

    def get(key: str) -> str | None:
        return _get_simple(desc, key)

    content_hash = get(f"{oc}contentHash")
    schema_ver_s = get(f"{oc}schemaVersion")
    metadata_ver_s = get(f"{oc}metadataVersion")
    media_type = get(f"{oc}mediaType") or "photo"
    duration_s = get(f"{oc}durationSeconds")
    video_codec = get(f"{oc}videoCodec") or None
    has_audio_s = get(f"{oc}hasAudio")
    date_s = get(f"{exif}DateTimeOriginal")
    make = get(f"{exif}Make") or get(f"{tiff}Make")
    model = get(f"{exif}Model") or get(f"{tiff}Model")
    orientation_s = get(f"{tiff}Orientation")
    rating_s = get(f"{xmp_ns}Rating")
    if rating_s is None:
        rating_s = get(f"{{{NS_XMP_LEGACY}}}Rating")
    width_s = get(f"{exif}PixelXDimension")
    height_s = get(f"{exif}PixelYDimension")

    gps = parse_xmp_gps(get(f"{exif}GPSLatitude"), get(f"{exif}GPSLongitude"))

    tags, excluded_tags = _read_tags(
        _read_li_list(desc.find(f"{{{NS_LR}}}hierarchicalSubject")),
        _read_li_list(desc.find(f"{dc}subject")),
        excluded_tag_prefixes,
    )

    # dc:description — LangAlt caption
    description: str | None = None
    desc_elem = desc.find(f"{dc}description")
    if desc_elem is not None:
        alt = desc_elem.find(f"{rdf}Alt")
        if alt is not None:
            li = alt.find(f"{rdf}li")
            description = li.text if li is not None else None
        else:
            description = desc_elem.text  # plain-string fallback

    # Shoot settings — simple rational/integer attributes
    def _rational_or_none(s: str | None) -> float | None:
        if not s:
            return None
        try:
            if "/" in s:
                n, d = s.split("/", 1)
                dv = int(d)
                return int(n) / dv if dv else None
            return float(s)
        except ValueError, TypeError, ZeroDivisionError:
            return None

    def _int_or_none(s: str | None) -> int | None:
        try:
            return int(s) if s is not None else None
        except ValueError, TypeError:
            return None

    aperture = _rational_or_none(get(f"{exif}FNumber"))
    exposure_time = _rational_or_none(get(f"{exif}ExposureTime"))
    focal_length = _rational_or_none(get(f"{exif}FocalLength"))
    focal_length_35mm = _int_or_none(get(f"{exif}FocalLengthIn35mmFilm"))
    lens_model = get(f"{exifex}LensModel") or get(f"{aux}Lens") or None

    # exif:ISOSpeedRatings — rdf:Seq of integers; take first value
    iso_speed: int | None = None
    iso_elem = desc.find(f"{exif}ISOSpeedRatings")
    if iso_elem is not None:
        seq = iso_elem.find(f"{rdf}Seq")
        li_elem = seq.find(f"{rdf}li") if seq is not None else iso_elem.find(f"{rdf}li")
        if li_elem is not None and li_elem.text:
            iso_speed = _int_or_none(li_elem.text.strip())

    # Collect unknown attributes and child elements into _extra.
    extra: dict[str, str] = {}
    for attr_key, attr_val in desc.attrib.items():
        if attr_key not in KNOWN_ATTRS:
            extra[attr_key] = attr_val
    for child in desc:
        if child.tag not in KNOWN_CHILDREN:
            extra[child.tag] = ET.tostring(child, encoding="unicode")

    return XmpSidecar(
        content_hash=content_hash,
        schema_version=int(schema_ver_s) if schema_ver_s else SCHEMA_VERSION,
        metadata_version=int(metadata_ver_s) if metadata_ver_s else 1,
        date_taken=parse_iso_datetime(date_s),
        camera_make=make or None,
        camera_model=model or None,
        orientation=_int_or_none(orientation_s),
        gps=gps,
        rating=_int_or_none(rating_s),
        width=_int_or_none(width_s),
        height=_int_or_none(height_s),
        tags=tags,
        description=description,
        iso_speed=iso_speed,
        aperture=aperture,
        exposure_time=exposure_time,
        focal_length=focal_length,
        focal_length_35mm=focal_length_35mm,
        lens_model=lens_model,
        media_type=media_type,
        duration_seconds=_rational_or_none(duration_s),
        video_codec=video_codec,
        has_audio=(has_audio_s == "true") if has_audio_s is not None else None,
        _extra=extra,
        _excluded_tags=excluded_tags,
    )
