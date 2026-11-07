"""Video domain object — identity and metadata extraction for a single video file.

Parallels ``photo.py`` rather than extending ``Photo``: video identity and
metadata come from container/stream inspection (PyAV) plus a decoded cover
frame, which differs enough from EXIF-based photo extraction to warrant its own
class. Both produce an :class:`XmpSidecar`, so downstream indexing/manifest code
stays uniform across media types.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ouestcharlie_toolkit.media.video.mp4 import display_rotation, read_moov_atom
from ouestcharlie_toolkit.media.video.timing import container_tag, parse_iso6709, resolve_video_time
from ouestcharlie_toolkit.model.entries import XmpSidecar
from ouestcharlie_toolkit.storage.backend import Backend
from ouestcharlie_toolkit.storage.hashing import content_hash

if TYPE_CHECKING:
    from pathlib import Path

    import av.container
    from PIL import Image

_log = logging.getLogger(__name__)

# Fraction into the clip to grab the cover frame — avoids black/transition frames
# common at frame 0 while staying cheap (one seek + one keyframe decode).
_COVER_FRAME_POSITION = 0.1


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
            header = read_moov_atom(local)
            cover = self._decode_cover_frame(local)
            self._content_hash = video_identity_hash(header, cover)
        return self._content_hash

    def extract_cover_frame(self, local_path: Path | None = None) -> Image.Image:
        """Decode a single representative cover frame as an upright PIL image.

        Seeks ~10% into the clip and decodes one frame — the only frame ever
        decoded (no full-video decode, no audio decode). PyAV does not apply the
        container's display-matrix rotation, so it is applied here: the returned
        image is oriented for display, matching what a video player would show.
        """
        if local_path is None:
            raise ValueError("local_path is required")
        image = self._decode_cover_frame(local_path)
        rotation = display_rotation(read_moov_atom(local_path))
        if rotation:
            # Display matrix encodes a clockwise rotation; PIL rotate() is CCW.
            image = image.rotate(-rotation, expand=True)
        return image

    async def extract_metadata(self) -> XmpSidecar:
        """Extract container/stream metadata into an XmpSidecar.

        Also caches the content hash so a subsequent ``create_identity()`` call
        does not re-open the file.

        Returns:
            XmpSidecar with ``media_type="video"`` and video/shared fields set.
        """
        import av  # lazy: native extension bundling ffmpeg libraries

        local = await self.backend.local_path(self.path)
        header = read_moov_atom(local)

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

        # Store display-oriented dimensions: a 90°/270° rotated video is stored
        # landscape but displays portrait, so swap to match the cover frame the
        # gallery renders and uses for aspect ratio.
        if display_rotation(header) in (90, 270) and width is not None and height is not None:
            width, height = height, width

        # Identity hashes the raw (unrotated) decoded frame, so it stays stable
        # regardless of the rotation-correction logic above.
        self._content_hash = video_identity_hash(header, cover)

        gps = parse_iso6709(
            container_tag(metadata, "com.apple.quicktime.location.ISO6709", "location")
        )
        # creation_time is UTC; resolve local wall-clock (offset from the Apple tag,
        # GPS, or a timestamped filename) so date_taken matches the naive-local
        # convention (OEC-18).
        filename = self.path.replace("\\", "/").rsplit("/", 1)[-1]
        date_taken = resolve_video_time(metadata, gps, filename=filename)
        camera_make = container_tag(metadata, "com.apple.quicktime.make", "make")
        camera_model = container_tag(metadata, "com.apple.quicktime.model", "model")

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
