"""Test XMP utilities, parsing, and serialization."""

import logging
from datetime import datetime
from pathlib import Path

from ouestcharlie_toolkit.schema import XmpSidecar
from ouestcharlie_toolkit.xmp import (
    _decimal_to_xmp_coord,
    _parse_iso_datetime,
    _parse_xmp_gps,
    _xmp_coord_to_decimal,
    parse_xmp,
    serialize_xmp,
    xmp_path_for,
)

_SAMPLES = Path(__file__).parent / "sample-images"

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def test_xmp_path_for_simple():
    """Test XMP path generation for simple filename."""
    xmp_path = xmp_path_for("photo.jpg")
    assert xmp_path == "photo.xmp"


def test_xmp_path_for_with_directory():
    """Test XMP path generation with directory path."""
    xmp_path = xmp_path_for("2024/IMG_001.jpg")
    assert xmp_path == "2024/IMG_001.xmp"


def test_xmp_path_for_nested():
    """Test XMP path generation for nested directories."""
    xmp_path = xmp_path_for("2024/2024-07/vacation/IMG_001.jpg")
    assert xmp_path == "2024/2024-07/vacation/IMG_001.xmp"


def test_xmp_path_for_different_extensions():
    """Test XMP path generation for various file extensions."""
    assert xmp_path_for("photo.JPG") == "photo.xmp"
    assert xmp_path_for("photo.dng") == "photo.xmp"
    assert xmp_path_for("photo.cr2") == "photo.xmp"
    assert xmp_path_for("photo.nef") == "photo.xmp"


def test_xmp_path_for_no_extension():
    """Test XMP path generation for files without extension."""
    xmp_path = xmp_path_for("photo")
    assert xmp_path == "photo.xmp"


def test_xmp_path_for_multiple_dots():
    """Test XMP path generation for filenames with multiple dots."""
    xmp_path = xmp_path_for("my.photo.backup.jpg")
    assert xmp_path == "my.photo.backup.xmp"


# ---------------------------------------------------------------------------
# Datetime helpers
# ---------------------------------------------------------------------------


def test_parse_iso_datetime_valid():
    assert _parse_iso_datetime("2024-07-15T14:30:00") == datetime(2024, 7, 15, 14, 30, 0)


def test_parse_iso_datetime_preserves_subseconds():
    assert _parse_iso_datetime("2024-07-15T14:30:00.123") == datetime(
        2024, 7, 15, 14, 30, 0, 123000
    )


def test_parse_iso_datetime_preserves_timezone():
    from datetime import timedelta, timezone

    dt = _parse_iso_datetime("2024-07-15T14:30:00.123+01:00")
    assert dt == datetime(2024, 7, 15, 14, 30, 0, 123000, tzinfo=timezone(timedelta(hours=1)))


def test_parse_iso_datetime_none():
    assert _parse_iso_datetime(None) is None


def test_parse_iso_datetime_empty():
    assert _parse_iso_datetime("") is None


def test_parse_iso_datetime_invalid():
    assert _parse_iso_datetime("not-a-date") is None


# ---------------------------------------------------------------------------
# GPS helpers
# ---------------------------------------------------------------------------


def test_xmp_coord_to_decimal_north():
    # 48°30'N = 48.5
    result = _xmp_coord_to_decimal("48,30.000000N")
    assert abs(result - 48.5) < 1e-6


def test_xmp_coord_to_decimal_south():
    result = _xmp_coord_to_decimal("48,30.000000S")
    assert abs(result - (-48.5)) < 1e-6


def test_xmp_coord_to_decimal_east():
    result = _xmp_coord_to_decimal("2,21.132000E")
    assert result > 0


def test_xmp_coord_to_decimal_west():
    result = _xmp_coord_to_decimal("2,21.132000W")
    assert result < 0


def test_decimal_to_xmp_coord_lat():
    s = _decimal_to_xmp_coord(48.5, is_lat=True)
    assert s.endswith("N")
    assert s.startswith("48,")


def test_decimal_to_xmp_coord_lon_negative():
    s = _decimal_to_xmp_coord(-2.352, is_lat=False)
    assert s.endswith("W")


def test_gps_roundtrip():
    """Decimal → XMP coord → decimal should be lossless within float precision."""
    lat, lon = 48.8566, 2.3522
    lat_s = _decimal_to_xmp_coord(lat, is_lat=True)
    lon_s = _decimal_to_xmp_coord(lon, is_lat=False)
    result = _parse_xmp_gps(lat_s, lon_s)
    assert result is not None
    assert abs(result[0] - lat) < 1e-5
    assert abs(result[1] - lon) < 1e-5


def test_parse_xmp_gps_none_when_missing():
    assert _parse_xmp_gps(None, "2,21.132000E") is None
    assert _parse_xmp_gps("48,30.000000N", None) is None
    assert _parse_xmp_gps(None, None) is None


# ---------------------------------------------------------------------------
# parse_xmp
# ---------------------------------------------------------------------------


_SAMPLE_XMP = """\
<?xpacket begin='\xef\xbb\xbf' id='W5M0MpCehiHzreSzNTczkc9d'?>
<x:xmpmeta xmlns:x='adobe:ns:meta/'>
  <rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>
    <rdf:Description rdf:about=''
      xmlns:ouestcharlie='http://ouestcharlie.app/ns/1.0/'
      xmlns:exif='http://ns.adobe.com/exif/1.0/'
      xmlns:tiff='http://ns.adobe.com/tiff/1.0/'
      xmlns:dc='http://purl.org/dc/elements/1.1/'
      ouestcharlie:contentHash='sha256:abc123'
      ouestcharlie:schemaVersion='1'
      ouestcharlie:metadataVersion='2'
      exif:DateTimeOriginal='2024-07-15T14:30:00'
      exif:Make='Canon'
      exif:Model='EOS R5'
      tiff:Orientation='1'>
      <exif:GPSLatitude>48,51.396000N</exif:GPSLatitude>
      <exif:GPSLongitude>2,21.132000E</exif:GPSLongitude>
      <dc:subject>
        <rdf:Bag>
          <rdf:li>vacation</rdf:li>
          <rdf:li>paris</rdf:li>
        </rdf:Bag>
      </dc:subject>
    </rdf:Description>
  </rdf:RDF>
</x:xmpmeta>
<?xpacket end='w'?>"""


def test_parse_xmp_content_hash():
    s = parse_xmp(_SAMPLE_XMP)
    assert s.content_hash == "sha256:abc123"


def test_parse_xmp_schema_metadata_version():
    s = parse_xmp(_SAMPLE_XMP)
    assert s.schema_version == 1
    assert s.metadata_version == 2


def test_parse_xmp_date():
    s = parse_xmp(_SAMPLE_XMP)
    assert s.date_taken == datetime(2024, 7, 15, 14, 30, 0)


def test_parse_xmp_camera():
    s = parse_xmp(_SAMPLE_XMP)
    assert s.camera_make == "Canon"
    assert s.camera_model == "EOS R5"


def test_parse_xmp_orientation():
    s = parse_xmp(_SAMPLE_XMP)
    assert s.orientation == 1


def test_parse_xmp_gps():
    s = parse_xmp(_SAMPLE_XMP)
    assert s.gps is not None
    lat, lon = s.gps
    assert abs(lat - 48.856) < 0.01
    assert abs(lon - 2.352) < 0.01


def test_parse_xmp_tags():
    s = parse_xmp(_SAMPLE_XMP)
    assert s.tags == ["vacation", "paris"]


def test_parse_xmp_known_fields_not_in_extra():
    """All fields in the sample XMP are known — _extra should be empty."""
    s = parse_xmp(_SAMPLE_XMP)
    assert s._extra == {}


_SAMPLE_XMP_WITH_RATING_AND_DIMS = """\
<?xpacket begin='\xef\xbb\xbf' id='W5M0MpCehiHzreSzNTczkc9d'?>
<x:xmpmeta xmlns:x='adobe:ns:meta/'>
  <rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>
    <rdf:Description rdf:about=''
      xmlns:ouestcharlie='http://ouestcharlie.app/ns/1.0/'
      xmlns:xmp='http://ns.adobe.com/xmp/1.0/'
      xmlns:exif='http://ns.adobe.com/exif/1.0/'
      ouestcharlie:contentHash='sha256:abc'
      ouestcharlie:schemaVersion='1'
      ouestcharlie:metadataVersion='1'
      xmp:Rating='3'
      exif:PixelXDimension='6000'
      exif:PixelYDimension='4000'/>
  </rdf:RDF>
</x:xmpmeta>
<?xpacket end='w'?>"""


def test_parse_xmp_rating():
    """xmp:Rating is parsed as a typed int field."""
    s = parse_xmp(_SAMPLE_XMP_WITH_RATING_AND_DIMS)
    assert s.rating == 3
    assert "{http://ns.adobe.com/xmp/1.0/}Rating" not in s._extra


def test_parse_xmp_rejected_rating():
    """xmp:Rating=-1 (rejected) is parsed correctly."""
    xml = _SAMPLE_XMP_WITH_RATING_AND_DIMS.replace("xmp:Rating='3'", "xmp:Rating='-1'")
    assert parse_xmp(xml).rating == -1


def test_parse_xmp_width_height():
    """exif:PixelXDimension / PixelYDimension are parsed as typed int fields."""
    s = parse_xmp(_SAMPLE_XMP_WITH_RATING_AND_DIMS)
    assert s.width == 6000
    assert s.height == 4000
    assert "{http://ns.adobe.com/exif/1.0/}PixelXDimension" not in s._extra
    assert "{http://ns.adobe.com/exif/1.0/}PixelYDimension" not in s._extra


def test_serialize_xmp_rating_round_trip():
    """rating survives parse → serialize → parse."""
    s = parse_xmp(_SAMPLE_XMP_WITH_RATING_AND_DIMS)
    assert s.rating == 3
    restored = parse_xmp(serialize_xmp(s))
    assert restored.rating == 3


def test_serialize_xmp_width_height_round_trip():
    """width and height survive parse → serialize → parse."""
    s = parse_xmp(_SAMPLE_XMP_WITH_RATING_AND_DIMS)
    restored = parse_xmp(serialize_xmp(s))
    assert restored.width == 6000
    assert restored.height == 4000


def test_serialize_xmp_none_rating_omits_field():
    """When rating is None, xmp:Rating is not written to XMP."""
    from ouestcharlie_toolkit.schema import XmpSidecar

    s = XmpSidecar(content_hash="sha256:x")
    assert s.rating is None
    xml = serialize_xmp(s)
    assert "Rating" not in xml


def test_serialize_xmp_none_dims_omit_fields():
    """When width/height are None, pixel dimensions are not written to XMP."""
    from ouestcharlie_toolkit.schema import XmpSidecar

    s = XmpSidecar(content_hash="sha256:x")
    xml = serialize_xmp(s)
    assert "PixelXDimension" not in xml
    assert "PixelYDimension" not in xml


_SAMPLE_XMP_WITH_EXTRAS = """\
<?xpacket begin='\xef\xbb\xbf' id='W5M0MpCehiHzreSzNTczkc9d'?>
<x:xmpmeta xmlns:x='adobe:ns:meta/'>
  <rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>
    <rdf:Description rdf:about=''
      xmlns:ouestcharlie='http://ouestcharlie.app/ns/1.0/'
      xmlns:xmp='http://ns.adobe.com/xmp/1.0/'
      xmlns:lr='http://ns.adobe.com/lightroom/1.0/'
      ouestcharlie:contentHash='sha256:abc123'
      ouestcharlie:schemaVersion='1'
      ouestcharlie:metadataVersion='1'
      xmp:Rating='4'>
      <lr:hierarchicalSubject>
        <rdf:Bag>
          <rdf:li>Europe|France|Paris</rdf:li>
        </rdf:Bag>
      </lr:hierarchicalSubject>
    </rdf:Description>
  </rdf:RDF>
</x:xmpmeta>
<?xpacket end='w'?>"""


def test_parse_xmp_preserves_unknown_attr():
    """Unknown simple attributes are stored in _extra;
    known fields (e.g. xmp:Rating) become typed."""
    s = parse_xmp(_SAMPLE_XMP_WITH_EXTRAS)
    # xmp:Rating is now a known field — stored on the typed attribute, not in _extra.
    assert s.rating == 4
    assert "{http://ns.adobe.com/xmp/1.0/}Rating" not in s._extra


def test_parse_xmp_preserves_unknown_child_element():
    """Unknown child elements
    (e.g. lr:hierarchicalSubject bag) are stored in _extra."""
    s = parse_xmp(_SAMPLE_XMP_WITH_EXTRAS)
    key = "{http://ns.adobe.com/lightroom/1.0/}hierarchicalSubject"
    assert key in s._extra
    assert s._extra[key].startswith("<")
    assert "Paris" in s._extra[key]


def test_serialize_xmp_roundtrip_preserves_extra():
    """parse → serialize → parse preserves _extra fields.

    Simple attribute values are compared exactly. Child element values are
    compared by key presence and content, not exact whitespace, because ET
    normalises indentation on re-serialization.
    """
    original = parse_xmp(_SAMPLE_XMP_WITH_EXTRAS)
    xml2 = serialize_xmp(original)
    restored = parse_xmp(xml2)

    assert set(restored._extra.keys()) == set(original._extra.keys())
    # xmp:Rating is a typed field — survives round-trip as sidecar.rating, not in _extra.
    assert restored.rating == original.rating
    assert "{http://ns.adobe.com/xmp/1.0/}Rating" not in restored._extra
    # Child element: content preserved (whitespace may differ after ET round-trip)
    lr_key = "{http://ns.adobe.com/lightroom/1.0/}hierarchicalSubject"
    assert "Paris" in restored._extra[lr_key]


def test_parse_xmp_minimal():
    """Minimal XMP with only required OuEstCharlie fields."""
    xml = (
        "<x:xmpmeta xmlns:x='adobe:ns:meta/'>"
        "<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>"
        "<rdf:Description rdf:about=''"
        " xmlns:ouestcharlie='http://ouestcharlie.app/ns/1.0/'"
        " ouestcharlie:contentHash='sha256:xyz'"
        " ouestcharlie:schemaVersion='1'"
        " ouestcharlie:metadataVersion='1'/>"
        "</rdf:RDF></x:xmpmeta>"
    )
    s = parse_xmp(xml)
    assert s.content_hash == "sha256:xyz"
    assert s.date_taken is None
    assert s.gps is None
    assert s.tags == []


def test_parse_xmp_invalid_xml():
    """Invalid XML returns a default XmpSidecar without raising."""
    s = parse_xmp("not valid xml <<<")
    assert s.content_hash is None
    assert s._extra == {}


# ---------------------------------------------------------------------------
# serialize_xmp
# ---------------------------------------------------------------------------


def test_serialize_xmp_fresh():
    """Serialize a fresh XmpSidecar (no _raw_xml) produces valid parseable XMP."""
    sidecar = XmpSidecar(
        content_hash="sha256:def456",
        schema_version=1,
        metadata_version=1,
        date_taken=datetime(2024, 7, 15, 14, 30, 0),
        camera_make="Nikon",
        camera_model="Z9",
        orientation=6,
        gps=(48.8566, 2.3522),
        tags=["street", "night"],
    )
    xml = serialize_xmp(sidecar)

    assert "<?xpacket" in xml
    assert "sha256:def456" in xml
    assert "Nikon" in xml
    assert "Z9" in xml
    assert "street" in xml
    assert "night" in xml


def test_serialize_xmp_roundtrip():
    """parse → serialize → parse should produce equivalent fields."""
    original = parse_xmp(_SAMPLE_XMP)
    xml2 = serialize_xmp(original)
    restored = parse_xmp(xml2)

    assert restored.content_hash == original.content_hash
    assert restored.schema_version == original.schema_version
    assert restored.date_taken == original.date_taken
    assert restored.camera_make == original.camera_make
    assert restored.camera_model == original.camera_model
    assert restored.orientation == original.orientation
    assert restored.tags == original.tags
    assert restored.gps is not None and original.gps is not None
    assert abs(restored.gps[0] - original.gps[0]) < 1e-4
    assert abs(restored.gps[1] - original.gps[1]) < 1e-4


def test_serialize_xmp_increments_metadata_version():
    """XmpStore.write increments metadata_version; serialize itself just writes what's set."""
    s = XmpSidecar(content_hash="sha256:aaa", metadata_version=3)
    xml = serialize_xmp(s)
    restored = parse_xmp(xml)
    assert restored.metadata_version == 3


def test_serialize_xmp_no_optional_fields():
    """Serializing with None optionals does not emit those attributes/elements."""
    s = XmpSidecar(content_hash="sha256:bbb")
    xml = serialize_xmp(s)
    assert "GPSLatitude" not in xml
    assert "DateTimeOriginal" not in xml
    assert "dc:subject" not in xml or "rdf:li" not in xml


def test_serialize_xmp_empty_tags_omits_subject():
    s = XmpSidecar(content_hash="sha256:ccc", tags=[])
    xml = serialize_xmp(s)
    assert "<rdf:li>" not in xml


# ---------------------------------------------------------------------------
# _extra preservation — real reference XMP (001.ref.xmp)
# ---------------------------------------------------------------------------

_TIFF = "http://ns.adobe.com/tiff/1.0/"
_EXIF = "http://ns.adobe.com/exif/1.0/"
_XAP = "http://ns.adobe.com/xap/1.0/"
_PS = "http://ns.adobe.com/photoshop/1.0/"


def _ref_xmp() -> XmpSidecar:
    return parse_xmp((_SAMPLES / "001.ref.xmp").read_text(encoding="utf-8"))


def test_ref_xmp_extra_simple_attrs():
    """Simple EXIF/TIFF/XMP attributes from Exiv2 land in _extra with correct values."""
    extra = _ref_xmp()._extra
    assert extra[f"{{{_TIFF}}}Software"] == "A566BXXS8BZA7"
    assert extra[f"{{{_TIFF}}}ImageWidth"] == "6112"
    assert extra[f"{{{_TIFF}}}ImageLength"] == "6112"
    assert extra[f"{{{_EXIF}}}ExposureTime"] == "20/10000"
    assert extra[f"{{{_EXIF}}}FNumber"] == "18000/10000"
    assert extra[f"{{{_EXIF}}}FocalLength"] == "554/100"
    assert extra[f"{{{_EXIF}}}FocalLengthIn35mmFilm"] == "23"
    assert extra[f"{{{_XAP}}}CreateDate"] == "2026-02-21T13:03:10.140"
    assert extra[f"{{{_PS}}}DateCreated"] == "2026-02-21T13:03:10.140"


def test_ref_xmp_extra_structured_elements():
    """Structured child elements (rdf:Seq, struct) are stored as XML strings in _extra."""
    extra = _ref_xmp()._extra
    iso_key = f"{{{_EXIF}}}ISOSpeedRatings"
    flash_key = f"{{{_EXIF}}}Flash"

    assert iso_key in extra
    assert extra[iso_key].startswith("<")
    assert "50" in extra[iso_key]  # ISO 50

    assert flash_key in extra
    assert extra[flash_key].startswith("<")
    assert "False" in extra[flash_key]  # Flash did not fire


def test_ref_xmp_extra_roundtrip():
    """parse → serialize → parse preserves all _extra keys and values from the ref XMP."""
    original = _ref_xmp()
    restored = parse_xmp(serialize_xmp(original))

    assert set(restored._extra.keys()) == set(original._extra.keys())
    # Spot-check a few values that survive the ET round-trip unchanged
    for key in [
        f"{{{_TIFF}}}Software",
        f"{{{_EXIF}}}FNumber",
        f"{{{_XAP}}}CreateDate",
    ]:
        assert restored._extra[key] == original._extra[key]
    # Complex elements: content preserved (ET normalises whitespace)
    assert "50" in restored._extra[f"{{{_EXIF}}}ISOSpeedRatings"]
    assert "False" in restored._extra[f"{{{_EXIF}}}Flash"]


# ---------------------------------------------------------------------------
# Logging behaviour
# ---------------------------------------------------------------------------

_XMP_LOGGER = "ouestcharlie_toolkit.xmp"


def test_parse_xmp_invalid_xml_logs_warning(caplog):
    """parse_xmp with malformed XML emits a WARNING with exc_info."""
    with caplog.at_level(logging.WARNING, logger=_XMP_LOGGER):
        parse_xmp("not valid xml <<<")
    assert any("Malformed XMP" in msg for msg in caplog.messages)
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert any(r.exc_info is not None for r in caplog.records)


def test_parse_iso_datetime_invalid_logs_debug(caplog):
    """_parse_iso_datetime with a bad string emits a DEBUG message."""
    with caplog.at_level(logging.DEBUG, logger=_XMP_LOGGER):
        _parse_iso_datetime("not-a-date")
    assert any("Could not parse XMP datetime" in msg for msg in caplog.messages)
    assert any(r.levelno == logging.DEBUG for r in caplog.records)


def test_parse_xmp_gps_invalid_logs_debug(caplog):
    """_parse_xmp_gps with a malformed coordinate emits a DEBUG message."""
    with caplog.at_level(logging.DEBUG, logger=_XMP_LOGGER):
        _parse_xmp_gps("not-a-coord", "2,21.132000E")
    assert any("Could not parse XMP GPS" in msg for msg in caplog.messages)
    assert any(r.levelno == logging.DEBUG for r in caplog.records)
