"""Tests for the Video domain class."""

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
