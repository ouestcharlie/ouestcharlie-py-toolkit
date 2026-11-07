"""Serialize an XmpSidecar to XMP XML."""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from fractions import Fraction

from ouestcharlie_toolkit.model.entries import XmpSidecar
from ouestcharlie_toolkit.model.tags import flatten, normalize_paths
from ouestcharlie_toolkit.xmp.namespaces import (
    FRESH_XMP_SHELL,
    NS_AUX,
    NS_DC,
    NS_EXIF,
    NS_LR,
    NS_OC,
    NS_RDF,
    NS_TIFF,
    NS_XML,
    NS_XMP,
    XPACKET_FOOTER,
    XPACKET_HEADER,
    find_description,
    register_et_namespaces,
    register_extra_ns,
)
from ouestcharlie_toolkit.xmp.values import decimal_to_xmp_coord

_log = logging.getLogger(__name__)


def _set_or_del(elem: ET.Element, key: str, value: str | None) -> None:
    """Set an attribute to value, or delete it when value is None."""
    if value is not None:
        elem.set(key, value)
    elif key in elem.attrib:
        del elem.attrib[key]


def serialize_xmp(sidecar: XmpSidecar) -> str:
    """Serialize an XmpSidecar to XMP XML.

    Always builds a fresh XMP document from FRESH_XMP_SHELL. Known fields are
    written as typed attributes/elements; _extra attributes and child elements
    are restored verbatim so that third-party fields (Lightroom ratings, darktable
    settings, …) survive the round-trip.

    Args:
        sidecar: XmpSidecar to serialize.

    Returns:
        XMP XML string with <?xpacket ?> wrappers.
    """
    register_et_namespaces()
    register_extra_ns(sidecar._extra)

    root = ET.fromstring(FRESH_XMP_SHELL)
    desc = find_description(root)
    if desc is None:
        raise ValueError("XMP document is missing rdf:Description")

    oc = f"{{{NS_OC}}}"
    exif_ns = f"{{{NS_EXIF}}}"
    tiff_ns = f"{{{NS_TIFF}}}"
    xmp_ns = f"{{{NS_XMP}}}"
    dc_ns = f"{{{NS_DC}}}"
    rdf_ns = f"{{{NS_RDF}}}"
    aux_ns = f"{{{NS_AUX}}}"

    # ouestcharlie:* control fields
    if sidecar.content_hash is not None:
        desc.set(f"{oc}contentHash", sidecar.content_hash)
    desc.set(f"{oc}schemaVersion", str(sidecar.schema_version))
    desc.set(f"{oc}metadataVersion", str(sidecar.metadata_version))

    # Video fields — only emitted for videos, so photo sidecars stay unchanged.
    if sidecar.media_type != "photo":
        desc.set(f"{oc}mediaType", sidecar.media_type)
        if sidecar.duration_seconds is not None:
            desc.set(f"{oc}durationSeconds", repr(sidecar.duration_seconds))
        if sidecar.video_codec is not None:
            desc.set(f"{oc}videoCodec", sidecar.video_codec)
        if sidecar.has_audio is not None:
            desc.set(f"{oc}hasAudio", "true" if sidecar.has_audio else "false")

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
        lat_e.text = decimal_to_xmp_coord(sidecar.gps[0], is_lat=True)
        lon_e = ET.SubElement(desc, f"{exif_ns}GPSLongitude")
        lon_e.text = decimal_to_xmp_coord(sidecar.gps[1], is_lat=False)

    # Tags, darktable-style: every path (flat ones included, since darktable reads
    # only lr:hierarchicalSubject when present) plus the preserved excluded paths
    # go to lr:hierarchicalSubject; every level of every user path to dc:subject.
    user_tags = normalize_paths(sidecar.tags)
    hierarchical = normalize_paths(user_tags + sidecar._excluded_tags)
    if hierarchical:
        h_el = ET.SubElement(desc, f"{{{NS_LR}}}hierarchicalSubject")
        bag = ET.SubElement(h_el, f"{rdf_ns}Bag")
        for path in hierarchical:
            li = ET.SubElement(bag, f"{rdf_ns}li")
            li.text = path
    if user_tags:
        subj = ET.SubElement(desc, f"{dc_ns}subject")
        bag = ET.SubElement(subj, f"{rdf_ns}Bag")
        for tag in flatten(user_tags):
            li = ET.SubElement(bag, f"{rdf_ns}li")
            li.text = tag

    # Caption as dc:description > rdf:Alt > rdf:li xml:lang="x-default"
    if sidecar.description:
        desc_el = ET.SubElement(desc, f"{dc_ns}description")
        alt_el = ET.SubElement(desc_el, f"{rdf_ns}Alt")
        li_el = ET.SubElement(alt_el, f"{rdf_ns}li")
        li_el.set(f"{{{NS_XML}}}lang", "x-default")
        li_el.text = sidecar.description

    # ISO as exif:ISOSpeedRatings > rdf:Seq > rdf:li
    if sidecar.iso_speed is not None:
        iso_el = ET.SubElement(desc, f"{exif_ns}ISOSpeedRatings")
        seq_el = ET.SubElement(iso_el, f"{rdf_ns}Seq")
        li_el = ET.SubElement(seq_el, f"{rdf_ns}li")
        li_el.text = str(sidecar.iso_speed)

    # Shoot settings as rational attributes
    def _float_to_rational(v: float) -> str:
        """Serialize a float as a rational string (best-effort, preserving common fractions)."""
        # Store with up to 6 decimal places of numerator precision.
        frac = Fraction(v).limit_denominator(1_000_000)
        return f"{frac.numerator}/{frac.denominator}"

    if sidecar.aperture is not None:
        desc.set(f"{exif_ns}FNumber", _float_to_rational(sidecar.aperture))
    if sidecar.exposure_time is not None:
        desc.set(f"{exif_ns}ExposureTime", _float_to_rational(sidecar.exposure_time))
    if sidecar.focal_length is not None:
        desc.set(f"{exif_ns}FocalLength", _float_to_rational(sidecar.focal_length))
    if sidecar.focal_length_35mm is not None:
        desc.set(f"{exif_ns}FocalLengthIn35mmFilm", str(sidecar.focal_length_35mm))
    if sidecar.lens_model is not None:
        desc.set(f"{aux_ns}Lens", sidecar.lens_model)

    # Restore unknown fields from _extra.
    # Values that start with "<" are serialized XML child elements; others are attributes.
    for key, val in sidecar._extra.items():
        if isinstance(val, list):
            # Serialize list values as rdf:Bag child elements.
            elem = ET.SubElement(desc, key)
            bag = ET.SubElement(elem, f"{rdf_ns}Bag")
            for item in val:
                li = ET.SubElement(bag, f"{rdf_ns}li")
                li.text = str(item)
        else:
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
    return f"{XPACKET_HEADER}{body}{XPACKET_FOOTER}"
