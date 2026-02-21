"""XMP sidecar store for reading and writing XMP files with optimistic concurrency."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Callable

from .backend import Backend
from .schema import (
    OUESTCHARLIE_NS,
    SCHEMA_VERSION,
    VersionConflictError,
    VersionToken,
    XmpSidecar,
)

# ---------------------------------------------------------------------------
# XMP namespace URIs and serialization constants
# ---------------------------------------------------------------------------

_NS_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
_NS_X = "adobe:ns:meta/"
_NS_OC = OUESTCHARLIE_NS
_NS_EXIF = "http://ns.adobe.com/exif/1.0/"
_NS_TIFF = "http://ns.adobe.com/tiff/1.0/"
_NS_DC = "http://purl.org/dc/elements/1.1/"

_XPACKET_HEADER = "<?xpacket begin='\xef\xbb\xbf' id='W5M0MpCehiHzreSzNTczkc9d'?>\n"
_XPACKET_FOOTER = "\n<?xpacket end='w'?>"

# Minimal well-formed XMP shell used when no _raw_xml exists.
_FRESH_XMP_SHELL = (
    f"<x:xmpmeta xmlns:x='{_NS_X}'>"
    f"<rdf:RDF xmlns:rdf='{_NS_RDF}'>"
    f"<rdf:Description rdf:about=''"
    f" xmlns:ouestcharlie='{_NS_OC}'"
    f" xmlns:exif='{_NS_EXIF}'"
    f" xmlns:tiff='{_NS_TIFF}'"
    f" xmlns:dc='{_NS_DC}'"
    f"/>"
    f"</rdf:RDF>"
    f"</x:xmpmeta>"
)


def _register_et_namespaces() -> None:
    ET.register_namespace("x", _NS_X)
    ET.register_namespace("rdf", _NS_RDF)
    ET.register_namespace("ouestcharlie", _NS_OC)
    ET.register_namespace("exif", _NS_EXIF)
    ET.register_namespace("tiff", _NS_TIFF)
    ET.register_namespace("dc", _NS_DC)


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
    return str(p.with_suffix(".xmp"))


# ---------------------------------------------------------------------------
# Datetime helpers
# ---------------------------------------------------------------------------


def _parse_iso_datetime(s: str | None) -> datetime | None:
    """Parse ISO 8601 datetime as written by our XMP serializer (2024-07-15T14:30:00)."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s[:19])
    except ValueError:
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
        sidecar.metadata_version += 1
        xml = serialize_xmp(sidecar)
        return await self.backend.write_conditional(xmp_path, xml.encode("utf-8"), expected_version)

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
                if attempt == max_retries:
                    raise

        raise RuntimeError("Unexpected control flow")


# ---------------------------------------------------------------------------
# XMP parsing
# ---------------------------------------------------------------------------


def parse_xmp(xml: str) -> XmpSidecar:
    """Parse XMP XML into an XmpSidecar.

    Stores the original XML in _raw_xml for round-trip preservation of
    unknown fields and namespaces.

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
        return XmpSidecar(_raw_xml=xml)

    desc = _find_description(root)
    if desc is None:
        return XmpSidecar(_raw_xml=xml)

    oc = f"{{{_NS_OC}}}"
    exif = f"{{{_NS_EXIF}}}"
    tiff = f"{{{_NS_TIFF}}}"
    dc = f"{{{_NS_DC}}}"
    rdf = f"{{{_NS_RDF}}}"

    content_hash = desc.get(f"{oc}contentHash")
    schema_ver_s = desc.get(f"{oc}schemaVersion")
    metadata_ver_s = desc.get(f"{oc}metadataVersion")
    date_s = desc.get(f"{exif}DateTimeOriginal")
    make = desc.get(f"{exif}Make") or desc.get(f"{tiff}Make")
    model = desc.get(f"{exif}Model") or desc.get(f"{tiff}Model")
    orientation_s = desc.get(f"{tiff}Orientation")

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

    return XmpSidecar(
        content_hash=content_hash,
        schema_version=int(schema_ver_s) if schema_ver_s else SCHEMA_VERSION,
        metadata_version=int(metadata_ver_s) if metadata_ver_s else 1,
        date_taken=_parse_iso_datetime(date_s),
        camera_make=make or None,
        camera_model=model or None,
        orientation=int(orientation_s) if orientation_s else None,
        gps=gps,
        tags=tags,
        _raw_xml=xml,
    )


# ---------------------------------------------------------------------------
# XMP serialization
# ---------------------------------------------------------------------------


def serialize_xmp(sidecar: XmpSidecar) -> str:
    """Serialize an XmpSidecar to XMP XML.

    When sidecar._raw_xml is set the existing document is used as the base so
    that unknown fields and namespaces from other applications are preserved.
    Otherwise a fresh XMP document is produced.

    Args:
        sidecar: XmpSidecar to serialize.

    Returns:
        XMP XML string with <?xpacket ?> wrappers.
    """
    _register_et_namespaces()

    base = _strip_xpacket(sidecar._raw_xml) if sidecar._raw_xml else _FRESH_XMP_SHELL
    try:
        root = ET.fromstring(base)
    except ET.ParseError:
        root = ET.fromstring(_FRESH_XMP_SHELL)

    desc = _find_description(root)
    if desc is None:
        raise ValueError("XMP document is missing rdf:Description")

    oc = f"{{{_NS_OC}}}"
    exif_ns = f"{{{_NS_EXIF}}}"
    tiff_ns = f"{{{_NS_TIFF}}}"
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
        sidecar.date_taken.isoformat(timespec="seconds") if sidecar.date_taken else None,
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

    # GPS as child elements
    _remove_children(desc, f"{exif_ns}GPSLatitude")
    _remove_children(desc, f"{exif_ns}GPSLongitude")
    if sidecar.gps is not None:
        lat_e = ET.SubElement(desc, f"{exif_ns}GPSLatitude")
        lat_e.text = _decimal_to_xmp_coord(sidecar.gps[0], is_lat=True)
        lon_e = ET.SubElement(desc, f"{exif_ns}GPSLongitude")
        lon_e.text = _decimal_to_xmp_coord(sidecar.gps[1], is_lat=False)

    # Tags as dc:subject > rdf:Bag > rdf:li
    _remove_children(desc, f"{dc_ns}subject")
    if sidecar.tags:
        subj = ET.SubElement(desc, f"{dc_ns}subject")
        bag = ET.SubElement(subj, f"{rdf_ns}Bag")
        for tag in sidecar.tags:
            li = ET.SubElement(bag, f"{rdf_ns}li")
            li.text = tag

    body = ET.tostring(root, encoding="unicode")
    return f"{_XPACKET_HEADER}{body}{_XPACKET_FOOTER}"


