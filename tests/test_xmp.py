"""Test XMP utilities, parsing, and serialization."""

from datetime import datetime

from ouestcharlie_toolkit.xmp import (
    _decimal_to_xmp_coord,
    _parse_iso_datetime,
    _parse_xmp_gps,
    _xmp_coord_to_decimal,
    parse_xmp,
    serialize_xmp,
    xmp_path_for,
)
from ouestcharlie_toolkit.schema import XmpSidecar

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


def test_parse_iso_datetime_truncates_to_seconds():
    """Extra sub-second or timezone suffix is ignored gracefully."""
    assert _parse_iso_datetime("2024-07-15T14:30:00.123") == datetime(2024, 7, 15, 14, 30, 0)


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
    """Unknown simple attributes (e.g. xmp:Rating) are stored in _extra."""
    s = parse_xmp(_SAMPLE_XMP_WITH_EXTRAS)
    assert "{http://ns.adobe.com/xmp/1.0/}Rating" in s._extra
    assert s._extra["{http://ns.adobe.com/xmp/1.0/}Rating"] == "4"


def test_parse_xmp_preserves_unknown_child_element():
    """Unknown child elements (e.g. lr:hierarchicalSubject bag) are stored in _extra."""
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
    # Simple attribute: exact match
    assert restored._extra["{http://ns.adobe.com/xmp/1.0/}Rating"] == "4"
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
