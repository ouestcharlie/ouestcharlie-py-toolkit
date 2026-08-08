"""Tests for the Video domain class."""

import struct
import tempfile
from datetime import datetime
from pathlib import Path

import av
import pytest
from PIL import Image as PILImage

from ouestcharlie_toolkit import VIDEO_SUFFIXES, Video, video_identity_hash
from ouestcharlie_toolkit.backends.local import LocalBackend
from ouestcharlie_toolkit.video import (
    _container_tag,
    _display_rotation,
    _matrix_rotation,
    _parse_creation_time,
    _parse_iso6709,
    _read_moov_atom,
)


def _write_sample_video(
    path: Path,
    *,
    frames: int = 15,
    width: int = 64,
    height: int = 48,
    with_audio: bool = False,
) -> None:
    """Synthesize a tiny MP4 (mpeg4 video, optional AAC audio) for tests.

    mpeg4 is built into every ffmpeg build PyAV bundles, unlike libx264, so this
    stays portable across PyAV wheels.
    """
    import numpy as np

    with av.open(str(path), "w") as container:
        vstream = container.add_stream("mpeg4", rate=30)
        vstream.width = width
        vstream.height = height
        vstream.pix_fmt = "yuv420p"
        astream = container.add_stream("aac", rate=44100) if with_audio else None
        for i in range(frames):
            img = PILImage.new("RGB", (width, height), ((i * 15) % 256, 40, 90))
            frame = av.VideoFrame.from_image(img)
            container.mux(vstream.encode(frame))
        container.mux(vstream.encode())  # flush
        if astream is not None:
            for _ in range(5):
                samples = np.zeros((2, 1024), dtype="float32")
                aframe = av.AudioFrame.from_ndarray(samples, format="fltp", layout="stereo")
                aframe.rate = 44100
                container.mux(astream.encode(aframe))
            container.mux(astream.encode())  # flush


# ---------------------------------------------------------------------------
# create_identity / extract_metadata
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_identity_returns_22_char_string():
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_sample_video(Path(tmpdir) / "clip.mp4")
        identity = await Video(LocalBackend(root=tmpdir), "clip.mp4").create_identity()
    assert len(identity) == 22
    assert all(
        c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for c in identity
    )


@pytest.mark.asyncio
async def test_create_identity_stable_and_cached():
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_sample_video(Path(tmpdir) / "clip.mp4")
        video = Video(LocalBackend(root=tmpdir), "clip.mp4")
        first = await video.create_identity()
        second = await video.create_identity()  # cached, no re-read
    assert first == second


@pytest.mark.asyncio
async def test_extract_metadata_basic_fields():
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_sample_video(Path(tmpdir) / "clip.mp4", frames=30)
        sidecar = await Video(LocalBackend(root=tmpdir), "clip.mp4").extract_metadata()
    assert sidecar.media_type == "video"
    assert sidecar.video_codec == "mpeg4"
    assert sidecar.width == 64
    assert sidecar.height == 48
    assert sidecar.has_audio is False
    assert sidecar.duration_seconds is not None
    assert sidecar.duration_seconds > 0
    assert len(sidecar.content_hash) == 22


@pytest.mark.asyncio
async def test_extract_metadata_detects_audio():
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_sample_video(Path(tmpdir) / "clip.mp4", with_audio=True)
        sidecar = await Video(LocalBackend(root=tmpdir), "clip.mp4").extract_metadata()
    assert sidecar.has_audio is True


@pytest.mark.asyncio
async def test_extract_metadata_caches_hash():
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_sample_video(Path(tmpdir) / "clip.mp4")
        video = Video(LocalBackend(root=tmpdir), "clip.mp4")
        sidecar = await video.extract_metadata()
        identity = await video.create_identity()
    assert sidecar.content_hash == identity


@pytest.mark.asyncio
async def test_cover_frame_is_rgb_image():
    with tempfile.TemporaryDirectory() as tmpdir:
        local = Path(tmpdir) / "clip.mp4"
        _write_sample_video(local)
        cover = Video(LocalBackend(root=tmpdir), "clip.mp4").extract_cover_frame(local)
    assert isinstance(cover, PILImage.Image)
    assert cover.size == (64, 48)


# ---------------------------------------------------------------------------
# moov atom reading
# ---------------------------------------------------------------------------


def test_read_moov_atom_returns_moov_bytes():
    with tempfile.TemporaryDirectory() as tmpdir:
        local = Path(tmpdir) / "clip.mp4"
        _write_sample_video(local)
        moov = _read_moov_atom(local)
    # A moov atom's bytes 4:8 spell "moov".
    assert moov[4:8] == b"moov"
    assert len(moov) >= 8


def test_read_moov_atom_missing_raises():
    with tempfile.TemporaryDirectory() as tmpdir:
        bogus = Path(tmpdir) / "bogus.mp4"
        bogus.write_bytes(b"\x00\x00\x00\x08ftyp")  # one atom, no moov
        with pytest.raises(ValueError, match="No moov atom"):
            _read_moov_atom(bogus)


def test_identity_changes_with_cover_frame():
    """Same header bytes, different cover pixels → different identity."""
    header = b"\x00\x00\x00\x08moov"
    red = PILImage.new("RGB", (8, 8), (255, 0, 0))
    blue = PILImage.new("RGB", (8, 8), (0, 0, 255))
    assert video_identity_hash(header, red) != video_identity_hash(header, blue)


# ---------------------------------------------------------------------------
# metadata tag parsing
# ---------------------------------------------------------------------------


def test_parse_iso6709():
    assert _parse_iso6709("+37.7858-122.4064+010.000/") == (37.7858, -122.4064)
    assert _parse_iso6709("") is None
    assert _parse_iso6709(None) is None
    assert _parse_iso6709("garbage") is None


def test_parse_creation_time():
    assert _parse_creation_time("2024-07-01T12:34:56.000000Z") == datetime.fromisoformat(
        "2024-07-01T12:34:56.000000+00:00"
    )
    assert _parse_creation_time(None) is None
    assert _parse_creation_time("not-a-date") is None


def test_container_tag_first_non_empty():
    meta = {"com.apple.quicktime.make": " Apple ", "make": "Other"}
    assert _container_tag(meta, "com.apple.quicktime.make", "make") == "Apple"
    assert _container_tag({"make": ""}, "com.apple.quicktime.make", "make") is None


def test_video_suffixes():
    assert ".mov" in VIDEO_SUFFIXES
    assert ".mp4" in VIDEO_SUFFIXES
    assert ".jpg" not in VIDEO_SUFFIXES


# ---------------------------------------------------------------------------
# display rotation (tkhd matrix parsing)
# ---------------------------------------------------------------------------


def _box(box_type: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + box_type + payload


# Rotation matrices as (a, b, c, d) in 16.16 fixed point; u=v=0, w=0x40000000.
_MATRICES = {
    0: (65536, 0, 0, 65536),
    90: (0, 65536, -65536, 0),
    180: (-65536, 0, 0, -65536),
    270: (0, -65536, 65536, 0),
}


def _tkhd(rotation: int) -> bytes:
    a, b, c, d = _MATRICES[rotation]
    matrix = struct.pack(">9i", a, b, 0, c, d, 0, 0, 0, 0x40000000)
    # version/flags(4) + v0 timing(20) + reserved/layer/alt/vol(16) + matrix(36) + w/h(8)
    payload = b"\x00" * 4 + b"\x00" * 20 + b"\x00" * 16 + matrix + b"\x00" * 8
    return _box(b"tkhd", payload)


def _hdlr(handler: bytes) -> bytes:
    # version/flags(4) + pre_defined(4) + handler_type(4) + reserved(12) + name(1)
    payload = b"\x00" * 4 + b"\x00" * 4 + handler + b"\x00" * 12 + b"\x00"
    return _box(b"hdlr", payload)


def _moov(rotation: int, handler: bytes = b"vide") -> bytes:
    trak = _box(b"trak", _tkhd(rotation) + _box(b"mdia", _hdlr(handler)))
    return _box(b"moov", trak)


def test_display_rotation_all_orientations():
    for deg in (0, 90, 180, 270):
        assert _display_rotation(_moov(deg)) == deg


def test_matrix_rotation_unit_matrices():
    assert _matrix_rotation(1, 0, 0, 1) == 0
    assert _matrix_rotation(0, 1, -1, 0) == 90
    assert _matrix_rotation(-1, 0, 0, -1) == 180
    assert _matrix_rotation(0, -1, 1, 0) == 270


def test_matrix_rotation_scale_normalized():
    """A rotation combined with uniform scaling still yields the right angle."""
    assert _matrix_rotation(0, 3, -3, 0) == 90
    assert _matrix_rotation(0, -0.5, 0.5, 0) == 270


def test_matrix_rotation_anamorphic_scale():
    """Different per-axis scale (av_display_rotation_get normalizes each column)."""
    assert _matrix_rotation(0, 2, -8, 0) == 90
    assert _matrix_rotation(-4, 0, 0, -2) == 180


def test_matrix_rotation_singular_is_zero():
    """A singular matrix (zero-scale column) maps to no rotation, not a crash."""
    assert _matrix_rotation(0, 0, 0, 0) == 0
    assert _matrix_rotation(1, 0, 0, 0) == 0


def test_matrix_rotation_snaps_to_nearest_quarter_turn():
    """A slight skew (e.g. 2°) rounds to the nearest quarter turn."""
    import math as _math

    a, b = _math.cos(_math.radians(2)), _math.sin(_math.radians(2))
    assert _matrix_rotation(a, b, -b, a) == 0


def test_display_rotation_ignores_non_video_track():
    # A rotated audio track must not be reported as the video rotation.
    assert _display_rotation(_moov(90, handler=b"soun")) == 0


def test_display_rotation_absent_is_zero():
    assert _display_rotation(b"\x00\x00\x00\x08moov") == 0


@pytest.mark.asyncio
async def test_extract_metadata_no_rotation_keeps_dims():
    """A synthesized (unrotated) video keeps its native landscape dimensions."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_sample_video(Path(tmpdir) / "clip.mp4")
        sidecar = await Video(LocalBackend(root=tmpdir), "clip.mp4").extract_metadata()
    assert (sidecar.width, sidecar.height) == (64, 48)
