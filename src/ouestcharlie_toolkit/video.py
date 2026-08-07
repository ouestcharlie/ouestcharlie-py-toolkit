"""Video domain object — identity and metadata extraction for a single video file.

Parallels ``photo.py`` rather than extending ``Photo``: video identity and
metadata come from container/stream inspection (PyAV) plus a decoded cover
frame, which differs enough from EXIF-based photo extraction to warrant its own
class. Both produce an :class:`XmpSidecar`, so downstream indexing/manifest code
stays uniform across media types.
"""

from __future__ import annotations

import logging
import struct
from datetime import datetime
from typing import TYPE_CHECKING

from .backend import Backend
from .hashing import content_hash
from .schema import XmpSidecar

if TYPE_CHECKING:
    from pathlib import Path

    import av.container
    from PIL import Image

_log = logging.getLogger(__name__)

# Container suffixes handled as video (V1: QuickTime MOV + MP4). Used the same
# caller-driven way as photo.py's _HEIF_SUFFIXES / Backend.list_files(suffixes=).
VIDEO_SUFFIXES: frozenset[str] = frozenset({".mov", ".mp4"})

# Fraction into the clip to grab the cover frame — avoids black/transition frames
# common at frame 0 while staying cheap (one seek + one keyframe decode).
_COVER_FRAME_POSITION = 0.1

# Cap on the container-header bytes hashed for identity. The moov sample tables
# grow with clip length but stay far below the media payload; on the rare
# overflow we hash the capped prefix (deterministic, still far stronger than
# scalar metadata). See #39 §3.
_MOOV_READ_CAP = 16 * 1024 * 1024


def video_identity_hash(header_bytes: bytes, cover_frame: Image.Image) -> str:
    """Return the identity hash for a video: BLAKE3 over container header + cover pixels.

    Hashes two bounded-cost inputs we already read/decode during extraction:
    the container header bytes (the ``moov`` atom — stream metadata plus the
    sample tables that fingerprint the exact edit/encode) concatenated with the
    decoded cover-frame's raw RGB pixels (ties identity to visible content).
    Same 22-char truncated-BLAKE3 output as the photo content hash.
    """
    frame = cover_frame if cover_frame.mode == "RGB" else cover_frame.convert("RGB")
    return content_hash(header_bytes + frame.tobytes())


def _read_moov_atom(path: Path) -> bytes:
    """Read the ``moov`` atom bytes from an ISO-BMFF (MP4/MOV) container.

    Scans top-level atoms by following each atom's size header — this handles
    non-faststart files where ``moov`` sits after ``mdat`` — and returns the
    ``moov`` atom (including its 8/16-byte header), capped at ``_MOOV_READ_CAP``.
    The GB-scale ``mdat`` payload is seeked over, never read.

    Raises:
        ValueError: If no ``moov`` atom is found (not a valid MP4/MOV).
    """
    with open(path, "rb") as f:
        while True:
            header = f.read(8)
            if len(header) < 8:
                break
            size, atom_type = struct.unpack(">I4s", header)
            header_len = 8
            if size == 1:
                # 64-bit extended size in the next 8 bytes.
                ext = f.read(8)
                if len(ext) < 8:
                    break
                size = struct.unpack(">Q", ext)[0]
                header_len = 16
            atom_start = f.tell() - header_len
            if atom_type == b"moov":
                f.seek(atom_start)
                read_len = size if size != 0 else _MOOV_READ_CAP
                return f.read(min(read_len, _MOOV_READ_CAP))
            if size == 0:
                # Atom extends to EOF (only legal for the last atom) and it isn't moov.
                break
            f.seek(atom_start + size)
    raise ValueError(f"No moov atom found in {path} — not a valid MP4/MOV container")


def _parse_creation_time(raw: str | None) -> datetime | None:
    """Parse a container ``creation_time`` tag (ISO-8601, often trailing 'Z')."""
    if not raw:
        return None
    try:
        # Python's fromisoformat handles the trailing 'Z' since 3.11.
        return datetime.fromisoformat(raw.strip())
    except ValueError:
        _log.debug("Could not parse video creation_time %r", raw, exc_info=True)
        return None


def _parse_iso6709(raw: str | None) -> tuple[float, float] | None:
    """Parse an ISO-6709 location string (e.g. '+37.7858-122.4064+010.000/') to (lat, lon).

    iPhone MOVs store GPS under ``com.apple.quicktime.location.ISO6709`` as
    consecutive signed decimal fields; only the first two (latitude, longitude)
    are used, altitude and any trailing CRS reference are ignored.
    """
    if not raw:
        return None
    import re

    nums = re.findall(r"[+-]\d+(?:\.\d+)?", raw)
    if len(nums) < 2:
        return None
    try:
        return (float(nums[0]), float(nums[1]))
    except ValueError:
        _log.debug("Could not parse ISO-6709 location %r", raw, exc_info=True)
        return None


def _container_tag(metadata: dict[str, str], *keys: str) -> str | None:
    """Return the first non-empty value among *keys* in a container metadata dict."""
    for key in keys:
        val = (metadata.get(key) or "").strip()
        if val:
            return val
    return None


class Video:
    """Represents a single video file in a backend.

    Mirrors :class:`Photo`'s interface:

    - ``create_identity()`` — content hash over container header + cover frame
    - ``extract_metadata()`` — container/stream metadata as an :class:`XmpSidecar`

    Both open the file; ``extract_metadata()`` caches the hash so a subsequent
    ``create_identity()`` call is free.
    """

    def __init__(self, backend: Backend, path: str) -> None:
        """
        Args:
            backend: Backend that owns the video file.
            path: Relative path to the video within the backend root.
        """
        self.backend = backend
        self.path = path
        self._content_hash: str | None = None

    async def create_identity(self) -> str:
        """Return the identity hash of this video (header + cover-frame based).

        Returns:
            22-character base64url string (BLAKE3 truncated to 128 bits).
        """
        if self._content_hash is None:
            local = await self.backend.local_path(self.path)
            header = _read_moov_atom(local)
            cover = self._decode_cover_frame(local)
            self._content_hash = video_identity_hash(header, cover)
        return self._content_hash

    def extract_cover_frame(self, local_path: Path | None = None) -> Image.Image:
        """Decode a single representative cover frame as a PIL image.

        Seeks ~10% into the clip and decodes one frame — the only frame ever
        decoded (no full-video decode, no audio decode).
        """
        if local_path is None:
            raise ValueError("local_path is required")
        return self._decode_cover_frame(local_path)

    async def extract_metadata(self) -> XmpSidecar:
        """Extract container/stream metadata into an XmpSidecar.

        Also caches the content hash so a subsequent ``create_identity()`` call
        does not re-open the file.

        Returns:
            XmpSidecar with ``media_type="video"`` and video/shared fields set.
        """
        import av  # lazy: native extension bundling ffmpeg libraries

        local = await self.backend.local_path(self.path)
        header = _read_moov_atom(local)

        duration_seconds: float | None = None
        video_codec: str | None = None
        width: int | None = None
        height: int | None = None
        has_audio: bool | None = None
        metadata: dict[str, str] = {}

        with av.open(str(local)) as container:
            if container.duration is not None:
                # container.duration is in av.time_base (AV_TIME_BASE = 1e6) units.
                duration_seconds = container.duration / av.time_base
            has_audio = bool(container.streams.audio)
            if container.streams.video:
                vstream = container.streams.video[0]
                video_codec = vstream.codec_context.name
                width = vstream.codec_context.width
                height = vstream.codec_context.height
            metadata = dict(container.metadata)
            cover = self._decode_cover_frame_from_container(container)

        self._content_hash = video_identity_hash(header, cover)

        date_taken = _parse_creation_time(_container_tag(metadata, "creation_time"))
        gps = _parse_iso6709(
            _container_tag(metadata, "com.apple.quicktime.location.ISO6709", "location")
        )
        camera_make = _container_tag(metadata, "com.apple.quicktime.make", "make")
        camera_model = _container_tag(metadata, "com.apple.quicktime.model", "model")

        return XmpSidecar(
            content_hash=self._content_hash,
            media_type="video",
            date_taken=date_taken,
            gps=gps,
            camera_make=camera_make,
            camera_model=camera_model,
            width=width,
            height=height,
            duration_seconds=duration_seconds,
            video_codec=video_codec,
            has_audio=has_audio,
        )

    def _decode_cover_frame(self, local_path: Path) -> Image.Image:
        import av  # lazy: native extension bundling ffmpeg libraries

        with av.open(str(local_path)) as container:
            return self._decode_cover_frame_from_container(container)

    def _decode_cover_frame_from_container(
        self, container: av.container.InputContainer
    ) -> Image.Image:
        """Seek ~10% into an open container and decode one frame to a PIL image."""
        import contextlib

        import av  # lazy: native extension bundling ffmpeg libraries

        stream = container.streams.video[0]
        # Seek to ~10% of the duration when known, expressed in the stream's time base.
        if container.duration is not None and stream.time_base is not None:
            target_sec = (container.duration / av.time_base) * _COVER_FRAME_POSITION
            offset = int(target_sec / stream.time_base)
            # Some short/streamed clips can't seek — fall back to decoding from frame 0.
            with contextlib.suppress(av.error.FFmpegError):
                container.seek(offset, stream=stream)
        for frame in container.decode(video=0):
            image: Image.Image = frame.to_image()  # type: ignore[no-untyped-call]
            return image
        raise ValueError(f"No decodable video frame in {container.name}")
