"""XMP sidecar store for reading and writing XMP files with optimistic concurrency."""

from __future__ import annotations

import contextlib
import logging
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .backend import Backend, VersionConflictError, VersionToken
from .schema import (
    METADATA_DIR,
    OUESTCHARLIE_NS,
    SCHEMA_VERSION,
    XmpSidecar,
)

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# XMP namespace URIs and serialization constants
# ---------------------------------------------------------------------------

_NS_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
_NS_X = "adobe:ns:meta/"
_NS_OC = OUESTCHARLIE_NS
_NS_EXIF = "http://ns.adobe.com/exif/1.0/"
_NS_TIFF = "http://ns.adobe.com/tiff/1.0/"
_NS_XMP = "http://ns.adobe.com/xmp/1.0/"
_NS_DC = "http://purl.org/dc/elements/1.1/"

_XPACKET_HEADER = "<?xpacket begin='\xef\xbb\xbf' id='W5M0MpCehiHzreSzNTczkc9d'?>\n"
_XPACKET_FOOTER = "\n<?xpacket end='w'?>"

# Minimal well-formed XMP shell — namespaces are declared by ET as fields are added.
_FRESH_XMP_SHELL = (
    f"<x:xmpmeta xmlns:x='{_NS_X}'>"
    f"<rdf:RDF xmlns:rdf='{_NS_RDF}'>"
    f"<rdf:Description rdf:about=''/>"
    f"</rdf:RDF>"
    f"</x:xmpmeta>"
)

# Known third-party namespaces → their conventional XMP prefix.
# Used so that ET serializes them with human-readable prefixes rather than ns0, ns1, …
_WELL_KNOWN_NS: dict[str, str] = {
    "http://ns.adobe.com/xmp/1.0/": "xmp",
    "http://ns.adobe.com/photoshop/1.0/": "photoshop",
    "http://ns.adobe.com/lightroom/1.0/": "lr",
    "http://ns.adobe.com/camera-raw-settings/1.0/": "crs",
    "http://ns.adobe.com/xap/1.0/mm/": "xmpMM",
    "http://darktable.sf.net/": "darktable",
    "http://iptc.org/std/Iptc4xmpCore/1.0/xmlns/": "Iptc4xmpCore",
    "http://ns.microsoft.com/photo/1.0/": "MicrosoftPhoto",
}

# Known rdf:Description attributes — not preserved in _extra.
_KNOWN_ATTRS: frozenset[str] = frozenset(
    {
        f"{{{_NS_RDF}}}about",
        f"{{{_NS_OC}}}contentHash",
        f"{{{_NS_OC}}}schemaVersion",
        f"{{{_NS_OC}}}metadataVersion",
        f"{{{_NS_EXIF}}}DateTimeOriginal",
        f"{{{_NS_EXIF}}}Make",
        f"{{{_NS_TIFF}}}Make",
        f"{{{_NS_EXIF}}}Model",
        f"{{{_NS_TIFF}}}Model",
        f"{{{_NS_TIFF}}}Orientation",
        f"{{{_NS_XMP}}}Rating",
        f"{{{_NS_EXIF}}}PixelXDimension",
        f"{{{_NS_EXIF}}}PixelYDimension",
    }
)

# Known rdf:Description child element tags — not preserved in _extra.
_KNOWN_CHILDREN: frozenset[str] = frozenset(
    {
        f"{{{_NS_EXIF}}}GPSLatitude",
        f"{{{_NS_EXIF}}}GPSLongitude",
        f"{{{_NS_DC}}}subject",
    }
)


def _register_et_namespaces() -> None:
    ET.register_namespace("x", _NS_X)
    ET.register_namespace("rdf", _NS_RDF)
    ET.register_namespace("ouestcharlie", _NS_OC)
    ET.register_namespace("exif", _NS_EXIF)
    ET.register_namespace("tiff", _NS_TIFF)
    ET.register_namespace("xmp", _NS_XMP)
    ET.register_namespace("dc", _NS_DC)
    for ns_uri, prefix in _WELL_KNOWN_NS.items():
        ET.register_namespace(prefix, ns_uri)


def _register_extra_ns(extra: dict[str, str]) -> None:
    """Register any namespace URIs found in _extra keys so ET uses proper prefixes."""
    seen: set[str] = set()
    counter = 0
    for key in extra:
        if not key.startswith("{"):
            continue
        ns_uri = key[1 : key.index("}")]
        if ns_uri in seen:
            continue
        seen.add(ns_uri)
        prefix = _WELL_KNOWN_NS.get(ns_uri)
        if prefix is None:
            prefix = f"ext{counter}"  # "ns\d+" is reserved by ET in Python 3.13+
            counter += 1
        ET.register_namespace(prefix, ns_uri)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def xmp_path_for(photo_path: str) -> str:
    """Compute the XMP sidecar path for a photo file.

    Args:
        photo_path: Path to the photo file (e.g., "2024/2024-07/IMG_001.jpg").

    Returns:
        Path to the XMP sidecar (e.g., "2024/2024-07/IMG_001.xmp").
    """
    p = Path(photo_path)
    return p.with_suffix(".xmp").as_posix()


def xmp_lock_dir_for(photo_path: str) -> str:
    """Compute the lock directory for a photo's XMP sidecar.

    Lock files are placed under the METADATA_DIR tree, mirroring the photo's
    partition, so they never appear next to original photo files.

    Args:
        photo_path: Path to the photo file (e.g., "2024/2024-07/IMG_001.jpg").

    Returns:
        Backend-relative lock directory (e.g., ".ouestcharlie/2024/2024-07").
    """
    parent = Path(photo_path).parent.as_posix()
    return f"{METADATA_DIR}/{parent}" if parent != "." else METADATA_DIR


# ---------------------------------------------------------------------------
# Datetime helpers
# ---------------------------------------------------------------------------


def _parse_iso_datetime(s: str | None) -> datetime | None:
    """Parse an ISO 8601 datetime string, preserving subseconds and timezone.

    Returns a timezone-aware datetime when an offset is present, a naive
    datetime otherwise.  Returns None for empty/invalid input.
    """
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        _log.debug("Could not parse XMP datetime %r", s, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# GPS helpers
# ---------------------------------------------------------------------------


def _xmp_coord_to_decimal(coord: str) -> float:
    """Convert XMP GPS coordinate '48,51.396000N' to decimal degrees."""
    ref = coord[-1]
    deg_min = coord[:-1].split(",")
    degrees = float(deg_min[0])
    minutes = float(deg_min[1]) if len(deg_min) > 1 else 0.0
    decimal = degrees + minutes / 60.0
    if ref in ("S", "W"):
        decimal = -decimal
    return decimal


def _decimal_to_xmp_coord(value: float, is_lat: bool) -> str:
    """Convert decimal degrees to XMP GPS format '48,51.396000N'."""
    ref = ("N" if value >= 0 else "S") if is_lat else ("E" if value >= 0 else "W")
    abs_val = abs(value)
    degrees = int(abs_val)
    minutes = (abs_val - degrees) * 60.0
    return f"{degrees},{minutes:.6f}{ref}"


def _parse_xmp_gps(lat_str: str | None, lon_str: str | None) -> tuple[float, float] | None:
    """Parse XMP GPS coordinate strings into (lat, lon) decimal degrees."""
    if not lat_str or not lon_str:
        return None
    try:
        return (_xmp_coord_to_decimal(lat_str), _xmp_coord_to_decimal(lon_str))
    except (ValueError, IndexError):
        _log.debug("Could not parse XMP GPS %r / %r", lat_str, lon_str, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# ElementTree helpers
# ---------------------------------------------------------------------------


def _strip_xpacket(xml: str) -> str:
    """Remove <?xpacket ...?> processing instructions before ET parsing."""
    lines = [line for line in xml.splitlines() if not line.strip().startswith("<?xpacket")]
    return "\n".join(lines).strip()


def _set_or_del(elem: ET.Element, key: str, value: str | None) -> None:
    """Set an attribute to value, or delete it when value is None."""
    if value is not None:
        elem.set(key, value)
    elif key in elem.attrib:
        del elem.attrib[key]


def _remove_children(elem: ET.Element, tag: str) -> None:
    """Remove all direct children with the given tag."""
    for child in elem.findall(tag):
        elem.remove(child)


def _find_description(root: ET.Element) -> ET.Element | None:
    rdf = root.find(f"{{{_NS_RDF}}}RDF")
    if rdf is None:
        return None
    return rdf.find(f"{{{_NS_RDF}}}Description")


# ---------------------------------------------------------------------------
# XMP store
# ---------------------------------------------------------------------------


class XmpStore:
    """Store for reading and writing XMP sidecar files with optimistic concurrency."""

    def __init__(self, backend: Backend) -> None:
        """Initialize the XMP store.

        Args:
            backend: Backend instance for storage operations.
        """
        self.backend = backend

    async def read(self, photo_path: str) -> tuple[XmpSidecar, VersionToken]:
        """Read an XMP sidecar and its version token.

        Args:
            photo_path: Path to the photo file.

        Returns:
            Tuple of (XmpSidecar, VersionToken).

        Raises:
            FileNotFoundError: If the XMP sidecar does not exist.
        """
        xmp_path = xmp_path_for(photo_path)
        data, version = await self.backend.read(xmp_path)
        sidecar = parse_xmp(data.decode("utf-8"))
        if not sidecar.content_hash:
            _log.warning(f"Empty identity for sidecar '{xmp_path}")
        return sidecar, version

    async def write(
        self, photo_path: str, sidecar: XmpSidecar, expected_version: VersionToken
    ) -> VersionToken:
        """Write an XMP sidecar with optimistic concurrency check.

        Automatically increments sidecar.metadata_version before writing.

        Args:
            photo_path: Path to the photo file.
            sidecar: XmpSidecar to write.
            expected_version: Expected version token.

        Returns:
            New version token after successful write.

        Raises:
            VersionConflictError: If the sidecar was modified since read.
        """
        xmp_path = xmp_path_for(photo_path)
        lock_dir = xmp_lock_dir_for(photo_path)
        sidecar.metadata_version += 1
        xml = serialize_xmp(sidecar)
        return await self.backend.write_conditional(
            xmp_path, xml.encode("utf-8"), expected_version, lock_dir
        )

    async def create(self, photo_path: str, sidecar: XmpSidecar) -> VersionToken:
        """Create a new XMP sidecar (fails if it already exists).

        Args:
            photo_path: Path to the photo file.
            sidecar: XmpSidecar to create.

        Returns:
            Version token of the newly created sidecar.

        Raises:
            FileExistsError: If the sidecar already exists.
        """
        xmp_path = xmp_path_for(photo_path)
        xml = serialize_xmp(sidecar)
        return await self.backend.write_new(xmp_path, xml.encode("utf-8"))

    async def read_or_create_from_picture(
        self,
        photo_path: str,
        force: bool = False,
    ) -> tuple[XmpSidecar, VersionToken, bool]:
        """Return the XMP sidecar for a photo, creating it from EXIF if needed.

        If an XMP sidecar already exists and ``force=False``, the existing
        sidecar is returned unchanged.  If the existing sidecar lacks
        ``ouestcharlie:contentHash`` (e.g. a third-party sidecar written by
        Lightroom), the hash is computed from the photo bytes and stored on the
        returned sidecar object without writing back.

        If no sidecar exists, or ``force=True``, EXIF is extracted from the
        photo file, a new sidecar is written, and the new sidecar is returned.

        Args:
            photo_path: Path to the photo file (relative to backend root).
            force: Re-extract EXIF and overwrite any existing sidecar.

        Returns:
            ``(sidecar, version_token, created)`` where ``created`` is ``True``
            when a new sidecar was written to the backend.
        """
        # Lazy import — photo.py does not import xmp.py, so no circular dep.
        from .photo import Photo

        existing_sidecar: XmpSidecar | None = None
        existing_version: VersionToken | None = None
        with contextlib.suppress(FileNotFoundError):
            existing_sidecar, existing_version = await self.read(photo_path)

        if existing_sidecar is not None and not force:
            if not existing_sidecar.content_hash:
                # Third-party sidecar without ouestcharlie:contentHash.
                existing_sidecar.content_hash = await Photo(
                    self.backend, photo_path
                ).create_identity()
            assert existing_version is not None
            return existing_sidecar, existing_version, False

        # Extract EXIF and write sidecar (new or forced overwrite).
        sidecar = await Photo(self.backend, photo_path).extract_exif()
        if existing_version is not None:
            new_version = await self.write(photo_path, sidecar, existing_version)
        else:
            new_version = await self.create(photo_path, sidecar)
        return sidecar, new_version, True

    async def read_modify_write(
        self,
        photo_path: str,
        modify: Callable[[XmpSidecar], XmpSidecar],
        max_retries: int = 3,
    ) -> XmpSidecar:
        """Read, modify, and write an XMP sidecar with retry on version conflict.

        Args:
            photo_path: Path to the photo file.
            modify: Function that takes an XmpSidecar and returns the modified version.
            max_retries: Maximum number of retries on version conflict.

        Returns:
            The successfully written XmpSidecar.

        Raises:
            VersionConflictError: If retries are exhausted.
            FileNotFoundError: If the sidecar does not exist.
        """
        for attempt in range(max_retries + 1):
            sidecar, version = await self.read(photo_path)
            updated = modify(sidecar)
            try:
                await self.write(photo_path, updated, version)
                return updated
            except VersionConflictError:
                _log.debug(
                    "Version conflict on %r (attempt %d/%d), retrying",
                    photo_path,
                    attempt + 1,
                    max_retries,
                )
                if attempt == max_retries:
                    raise

        raise RuntimeError("Unexpected control flow")


# ---------------------------------------------------------------------------
# XMP parsing
# ---------------------------------------------------------------------------


def parse_xmp(xml: str) -> XmpSidecar:
    """Parse XMP XML into an XmpSidecar.

    Known fields are mapped to typed XmpSidecar attributes. Unknown attributes
    and child elements on rdf:Description are stored in _extra for round-trip
    preservation (same pattern as manifest _extra dicts).

    Args:
        xml: XMP XML string (with or without <?xpacket ?> wrappers).

    Returns:
        XmpSidecar populated from the XMP data.
    """
    _register_et_namespaces()
    body = _strip_xpacket(xml)

    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        _log.warning("Malformed XMP document, returning empty sidecar", exc_info=True)
        return XmpSidecar()

    desc = _find_description(root)
    if desc is None:
        return XmpSidecar()

    oc = f"{{{_NS_OC}}}"
    exif = f"{{{_NS_EXIF}}}"
    tiff = f"{{{_NS_TIFF}}}"
    xmp_ns = f"{{{_NS_XMP}}}"
    dc = f"{{{_NS_DC}}}"
    rdf = f"{{{_NS_RDF}}}"

    content_hash = desc.get(f"{oc}contentHash")
    schema_ver_s = desc.get(f"{oc}schemaVersion")
    metadata_ver_s = desc.get(f"{oc}metadataVersion")
    date_s = desc.get(f"{exif}DateTimeOriginal")
    make = desc.get(f"{exif}Make") or desc.get(f"{tiff}Make")
    model = desc.get(f"{exif}Model") or desc.get(f"{tiff}Model")
    orientation_s = desc.get(f"{tiff}Orientation")
    rating_s = desc.get(f"{xmp_ns}Rating")
    width_s = desc.get(f"{exif}PixelXDimension")
    height_s = desc.get(f"{exif}PixelYDimension")

    lat_elem = desc.find(f"{exif}GPSLatitude")
    lon_elem = desc.find(f"{exif}GPSLongitude")
    gps = _parse_xmp_gps(
        lat_elem.text if lat_elem is not None else None,
        lon_elem.text if lon_elem is not None else None,
    )

    tags: list[str] = []
    subject = desc.find(f"{dc}subject")
    if subject is not None:
        bag = subject.find(f"{rdf}Bag")
        if bag is not None:
            tags = [li.text or "" for li in bag.findall(f"{rdf}li")]

    def _int_or_none(s: str | None) -> int | None:
        try:
            return int(s) if s is not None else None
        except (ValueError, TypeError):
            return None

    # Collect unknown attributes and child elements into _extra.
    extra: dict[str, str] = {}
    for attr_key, attr_val in desc.attrib.items():
        if attr_key not in _KNOWN_ATTRS:
            extra[attr_key] = attr_val
    for child in desc:
        if child.tag not in _KNOWN_CHILDREN:
            extra[child.tag] = ET.tostring(child, encoding="unicode")

    return XmpSidecar(
        content_hash=content_hash,
        schema_version=int(schema_ver_s) if schema_ver_s else SCHEMA_VERSION,
        metadata_version=int(metadata_ver_s) if metadata_ver_s else 1,
        date_taken=_parse_iso_datetime(date_s),
        camera_make=make or None,
        camera_model=model or None,
        orientation=_int_or_none(orientation_s),
        gps=gps,
        rating=_int_or_none(rating_s),
        width=_int_or_none(width_s),
        height=_int_or_none(height_s),
        tags=tags,
        _extra=extra,
    )


# ---------------------------------------------------------------------------
# XMP serialization
# ---------------------------------------------------------------------------


def serialize_xmp(sidecar: XmpSidecar) -> str:
    """Serialize an XmpSidecar to XMP XML.

    Always builds a fresh XMP document from _FRESH_XMP_SHELL. Known fields are
    written as typed attributes/elements; _extra attributes and child elements
    are restored verbatim so that third-party fields (Lightroom ratings, darktable
    settings, …) survive the round-trip.

    Args:
        sidecar: XmpSidecar to serialize.

    Returns:
        XMP XML string with <?xpacket ?> wrappers.
    """
    _register_et_namespaces()
    _register_extra_ns(sidecar._extra)

    root = ET.fromstring(_FRESH_XMP_SHELL)
    desc = _find_description(root)
    if desc is None:
        raise ValueError("XMP document is missing rdf:Description")

    oc = f"{{{_NS_OC}}}"
    exif_ns = f"{{{_NS_EXIF}}}"
    tiff_ns = f"{{{_NS_TIFF}}}"
    xmp_ns = f"{{{_NS_XMP}}}"
    dc_ns = f"{{{_NS_DC}}}"
    rdf_ns = f"{{{_NS_RDF}}}"

    # ouestcharlie:* control fields
    if sidecar.content_hash is not None:
        desc.set(f"{oc}contentHash", sidecar.content_hash)
    desc.set(f"{oc}schemaVersion", str(sidecar.schema_version))
    desc.set(f"{oc}metadataVersion", str(sidecar.metadata_version))

    # Date (ISO 8601)
    _set_or_del(
        desc,
        f"{exif_ns}DateTimeOriginal",
        sidecar.date_taken.isoformat() if sidecar.date_taken else None,
    )

    # Camera
    _set_or_del(desc, f"{exif_ns}Make", sidecar.camera_make)
    _set_or_del(desc, f"{exif_ns}Model", sidecar.camera_model)

    # Orientation
    _set_or_del(
        desc,
        f"{tiff_ns}Orientation",
        str(sidecar.orientation) if sidecar.orientation is not None else None,
    )

    # Rating (xmp:Rating — standard XMP field; -1=rejected, 0=unrated, 1-5=stars)
    _set_or_del(
        desc,
        f"{xmp_ns}Rating",
        str(sidecar.rating) if sidecar.rating is not None else None,
    )

    # Pixel dimensions
    _set_or_del(
        desc,
        f"{exif_ns}PixelXDimension",
        str(sidecar.width) if sidecar.width is not None else None,
    )
    _set_or_del(
        desc,
        f"{exif_ns}PixelYDimension",
        str(sidecar.height) if sidecar.height is not None else None,
    )

    # GPS as child elements
    if sidecar.gps is not None:
        lat_e = ET.SubElement(desc, f"{exif_ns}GPSLatitude")
        lat_e.text = _decimal_to_xmp_coord(sidecar.gps[0], is_lat=True)
        lon_e = ET.SubElement(desc, f"{exif_ns}GPSLongitude")
        lon_e.text = _decimal_to_xmp_coord(sidecar.gps[1], is_lat=False)

    # Tags as dc:subject > rdf:Bag > rdf:li
    if sidecar.tags:
        subj = ET.SubElement(desc, f"{dc_ns}subject")
        bag = ET.SubElement(subj, f"{rdf_ns}Bag")
        for tag in sidecar.tags:
            li = ET.SubElement(bag, f"{rdf_ns}li")
            li.text = tag

    # Restore unknown fields from _extra.
    # Values that start with "<" are serialized XML child elements; others are attributes.
    for key, val in sidecar._extra.items():
        if val.startswith("<"):
            try:
                child = ET.fromstring(val)
                desc.append(child)
            except ET.ParseError:
                _log.warning(
                    "Skipping malformed _extra element for key %r: %r",
                    key,
                    val,
                    exc_info=True,
                )
        else:
            desc.set(key, val)

    body = ET.tostring(root, encoding="unicode")
    return f"{_XPACKET_HEADER}{body}{_XPACKET_FOOTER}"
