"""XMP namespace URIs, document shell and ElementTree prefix registration."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from ouestcharlie_toolkit.model.version import OUESTCHARLIE_NS

NS_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
NS_X = "adobe:ns:meta/"
NS_OC = OUESTCHARLIE_NS
NS_EXIF = "http://ns.adobe.com/exif/1.0/"
NS_TIFF = "http://ns.adobe.com/tiff/1.0/"
NS_XMP = "http://ns.adobe.com/xap/1.0/"  # XMP Basic: xmp:Rating
# Wrong XMP Basic URI written by OuEstCharlie before OEC-63: read, never written.
NS_XMP_LEGACY = "http://ns.adobe.com/xmp/1.0/"
NS_DC = "http://purl.org/dc/elements/1.1/"
NS_AUX = "http://ns.adobe.com/exif/1.0/aux/"  # Adobe aux:Lens
NS_EXIFEX = "http://cipa.jp/exif/1.0/"  # CIPA exifEX:LensModel
NS_LR = "http://ns.adobe.com/lightroom/1.0/"  # lr:hierarchicalSubject
NS_XML = "http://www.w3.org/XML/1998/namespace"  # xml:lang on rdf:li

XPACKET_HEADER = "<?xpacket begin='﻿' id='W5M0MpCehiHzreSzNTczkc9d'?>\n"
XPACKET_FOOTER = "\n<?xpacket end='w'?>"

# Minimal well-formed XMP shell — namespaces are declared by ET as fields are added.
FRESH_XMP_SHELL = (
    f"<x:xmpmeta xmlns:x='{NS_X}'>"
    f"<rdf:RDF xmlns:rdf='{NS_RDF}'>"
    f"<rdf:Description rdf:about=''/>"
    f"</rdf:RDF>"
    f"</x:xmpmeta>"
)

# Known third-party namespaces → their conventional XMP prefix.
# Used so that ET serializes them with human-readable prefixes rather than ns0, ns1, …
_WELL_KNOWN_NS: dict[str, str] = {
    NS_XMP: "xmp",
    "http://ns.adobe.com/photoshop/1.0/": "photoshop",
    NS_LR: "lr",
    "http://ns.adobe.com/camera-raw-settings/1.0/": "crs",
    "http://ns.adobe.com/xap/1.0/mm/": "xmpMM",
    "http://darktable.sf.net/": "darktable",
    "http://iptc.org/std/Iptc4xmpCore/1.0/xmlns/": "Iptc4xmpCore",
    "http://ns.microsoft.com/photo/1.0/": "MicrosoftPhoto",
    "http://ns.adobe.com/exif/1.0/aux/": "aux",
    "http://cipa.jp/exif/1.0/": "exifEX",
}

# Known simple properties. XMP allows a simple property either as an attribute
# of rdf:Description (Exiv2/darktable's form) or as a child element, so they are
# read in both forms and known in both forms (neither lands in _extra).
_SIMPLE_PROPS: frozenset[str] = frozenset(
    {
        f"{{{NS_OC}}}contentHash",
        f"{{{NS_OC}}}schemaVersion",
        f"{{{NS_OC}}}metadataVersion",
        # Video fields (ouestcharlie namespace — not standard XMP)
        f"{{{NS_OC}}}mediaType",
        f"{{{NS_OC}}}durationSeconds",
        f"{{{NS_OC}}}videoCodec",
        f"{{{NS_OC}}}hasAudio",
        f"{{{NS_EXIF}}}DateTimeOriginal",
        f"{{{NS_EXIF}}}Make",
        f"{{{NS_TIFF}}}Make",
        f"{{{NS_EXIF}}}Model",
        f"{{{NS_TIFF}}}Model",
        f"{{{NS_TIFF}}}Orientation",
        f"{{{NS_XMP}}}Rating",
        f"{{{NS_EXIF}}}PixelXDimension",
        f"{{{NS_EXIF}}}PixelYDimension",
        # Shoot settings (simple attributes)
        f"{{{NS_EXIF}}}FNumber",
        f"{{{NS_EXIF}}}ExposureTime",
        f"{{{NS_EXIF}}}FocalLength",
        f"{{{NS_EXIF}}}FocalLengthIn35mmFilm",
        f"{{{NS_AUX}}}Lens",
        f"{{{NS_EXIFEX}}}LensModel",
        f"{{{NS_EXIF}}}GPSLatitude",
        f"{{{NS_EXIF}}}GPSLongitude",
        # Legacy rating: read as a fallback, dropped on write.
        f"{{{NS_XMP_LEGACY}}}Rating",
    }
)

# Known rdf:Description attributes — not preserved in _extra.
KNOWN_ATTRS: frozenset[str] = _SIMPLE_PROPS | {f"{{{NS_RDF}}}about"}

# Known rdf:Description child element tags — not preserved in _extra.
KNOWN_CHILDREN: frozenset[str] = _SIMPLE_PROPS | frozenset(
    {
        f"{{{NS_DC}}}subject",
        f"{{{NS_LR}}}hierarchicalSubject",  # rdf:Bag of "|"-separated tag paths
        f"{{{NS_DC}}}description",  # LangAlt caption
        f"{{{NS_EXIF}}}ISOSpeedRatings",  # rdf:Seq of ints
    }
)


def register_et_namespaces() -> None:
    ET.register_namespace("x", NS_X)
    ET.register_namespace("rdf", NS_RDF)
    ET.register_namespace("ouestcharlie", NS_OC)
    ET.register_namespace("exif", NS_EXIF)
    ET.register_namespace("tiff", NS_TIFF)
    ET.register_namespace("xmp", NS_XMP)
    ET.register_namespace("dc", NS_DC)
    ET.register_namespace("aux", NS_AUX)
    ET.register_namespace("exifEX", NS_EXIFEX)
    for ns_uri, prefix in _WELL_KNOWN_NS.items():
        ET.register_namespace(prefix, ns_uri)


def register_extra_ns(extra: dict[str, str]) -> None:
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


def find_description(root: ET.Element) -> ET.Element | None:
    rdf = root.find(f"{{{NS_RDF}}}RDF")
    if rdf is None:
        return None
    return rdf.find(f"{{{NS_RDF}}}Description")
