"""Video domain object — identity and metadata extraction for a single video file.

Parallels ``photo.py`` rather than extending ``Photo``: video identity and
metadata come from container/stream inspection (PyAV) plus a decoded cover
frame, which differs enough from EXIF-based photo extraction to warrant its own
class. Both produce an :class:`XmpSidecar`, so downstream indexing/manifest code
stays uniform across media types.
"""

from __future__ import annotations

import logging
import math
import re
import struct
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from typing import TYPE_CHECKING

from .backend import Backend
from .filename_time import date_from_filename, datetime_from_filename
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


def _iter_boxes(buf: bytes, start: int, end: int) -> Iterator[tuple[bytes, int, int]]:
    """Yield ``(type, box_start, box_end)`` for each ISO-BMFF box in ``buf[start:end]``."""
    off = start
    while off + 8 <= end:
        size = struct.unpack(">I", buf[off : off + 4])[0]
        box_type = buf[off + 4 : off + 8]
        if size == 1:  # 64-bit extended size
            if off + 16 > end:
                break
            size = struct.unpack(">Q", buf[off + 8 : off + 16])[0]
        if size == 0:  # extends to end
            size = end - off
        if size < 8:
            break
        yield box_type, off, min(off + size, end)
        off += size


def _matrix_rotation(a: float, b: float, c: float, d: float) -> int:
    """Display rotation in degrees (0/90/180/270) from a 2×2 transform, snapped
    to the nearest quarter turn.

    Ports FFmpeg's ``av_display_rotation_get`` (libavutil/display.c): each column
    is scale-normalized (``hypot``) before the angle is taken, so a matrix that
    also encodes (possibly anamorphic) scaling still yields the correct rotation.
    ``av_display_rotation_get`` returns the counter-clockwise angle the matrix
    applies and is undefined for a singular matrix; here that maps to the
    clockwise rotation needed to display the frame upright, with a singular
    matrix (zero-scale column) treated as no rotation.
    """
    scale_x = math.hypot(a, c)
    scale_y = math.hypot(b, d)
    if scale_x == 0.0 or scale_y == 0.0:
        return 0  # singular matrix — av_display_rotation_get returns NaN
    rotation = math.degrees(math.atan2(b / scale_y, a / scale_x))
    return round(rotation / 90) % 4 * 90


def _tkhd_rotation(buf: bytes, start: int, end: int) -> int:
    """Extract the display rotation (0/90/180/270) from a ``tkhd`` box's 3×3 matrix.

    The matrix is located by its ``w`` element (``0x40000000`` in 2.30 fixed
    point) with zero ``u``/``v`` perspective terms — robust to the
    version-dependent field layout that precedes it. Rotation is then derived
    from the ``a``/``b``/``c``/``d`` terms (16.16 fixed point) via
    ``_matrix_rotation``.
    """
    for off in range(start, min(end, start + 80), 4):
        if off + 36 > len(buf):
            break
        m = struct.unpack(">9i", buf[off : off + 36])
        if m[8] == 0x40000000 and m[2] == 0 and m[5] == 0:
            return _matrix_rotation(m[0] / 65536, m[1] / 65536, m[3] / 65536, m[4] / 65536)
    return 0


def _display_rotation(moov: bytes) -> int:
    """Return the video track's display rotation in degrees (0/90/180/270).

    Walks ``moov`` for the ``trak`` whose media handler is ``vide`` and reads its
    ``tkhd`` transformation matrix. Returns 0 when absent or unrotated.
    """
    for box_type, s, e in _iter_boxes(moov, 8, len(moov)):
        if box_type != b"trak":
            continue
        handler: bytes | None = None
        rotation = 0
        for t2, s2, e2 in _iter_boxes(moov, s + 8, e):
            if t2 == b"tkhd":
                rotation = _tkhd_rotation(moov, s2 + 8, e2)
            elif t2 == b"mdia":
                for t3, s3, _e3 in _iter_boxes(moov, s2 + 8, e2):
                    if t3 == b"hdlr":
                        handler = moov[s3 + 16 : s3 + 20]
        if handler == b"vide":
            return rotation
    return 0


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
    nums = re.findall(r"[+-]\d+(?:\.\d+)?", raw)
    if len(nums) < 2:
        return None
    try:
        return (float(nums[0]), float(nums[1]))
    except ValueError:
        _log.debug("Could not parse ISO-6709 location %r", raw, exc_info=True)
        return None


def _parse_utc_offset(raw: str | None) -> timezone | None:
    """Parse a fixed UTC offset string (e.g. '+0100', '-08:00', 'Z') to a timezone.

    Android vendors record the capture offset in a container tag
    (``com.samsung.android.utc_offset`` = ``+0100``).
    Returns None when the value is absent or unparseable.
    """
    if not raw:
        return None
    raw = raw.strip()
    if raw in ("Z", "+0000", "+00:00", "-0000", "-00:00"):
        return UTC
    m = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", raw)
    if m is None:
        return None
    sign = 1 if m.group(1) == "+" else -1
    minutes = sign * (int(m.group(2)) * 60 + int(m.group(3)))
    if abs(minutes) > 14 * 60:  # outside the valid UTC-offset range
        return None
    return timezone(timedelta(minutes=minutes))


def _timezone_for(gps: tuple[float, float]) -> tzinfo | None:
    """Resolve an IANA timezone from (lat, lon) coordinates, or None.

    Uses ``tzfpy`` for the offline coordinate→zone lookup; returns None when the
    dependency is unavailable or the point falls outside any zone (e.g. open
    ocean), so the caller can fall back to the offset-unknown case.
    """
    try:
        from tzfpy import get_tz
    except ImportError:
        _log.debug("tzfpy not installed; cannot derive video timezone from GPS")
        return None
    lat, lon = gps
    tz_name = get_tz(lon, lat)  # tzfpy takes (longitude, latitude)
    if not tz_name:
        return None
    from zoneinfo import ZoneInfo

    try:
        return ZoneInfo(tz_name)
    except Exception:  # pragma: no cover - malformed/unknown zone name
        _log.debug("Unknown timezone %r from GPS %r", tz_name, gps, exc_info=True)
        return None


# A filename-derived offset must land within this many minutes of a whole
# quarter-hour to be trusted (real zone offsets are all multiples of 15 min; the
# few-second slack between "recording started" in the name and the container's
# finalize time stays well under this).
_FILENAME_OFFSET_TOLERANCE_MIN = 5


def _offset_from_filename(filename: str | None, utc_dt: datetime) -> timezone | None:
    """Derive a fixed UTC offset from a timestamped filename against the UTC instant.

    Many camera apps name clips with the **local** wall-clock
    (``YYYYMMDD_HHMMSS``) while the container's ``creation_time`` is UTC. Their
    difference is the capture offset. Only trusted when it rounds to a whole
    quarter-hour within :data:`_FILENAME_OFFSET_TOLERANCE_MIN` and stays inside
    the valid ±14h UTC-offset range; otherwise (a renamed file, a name that isn't
    local time) returns None so the caller falls through to the UTC fallback.
    """
    local = datetime_from_filename(filename)
    if local is None:
        return None
    diff_min = (local - utc_dt.astimezone(UTC).replace(tzinfo=None)).total_seconds() / 60
    rounded = round(diff_min / 15) * 15
    if abs(diff_min - rounded) > _FILENAME_OFFSET_TOLERANCE_MIN or abs(rounded) > 14 * 60:
        return None
    return timezone(timedelta(minutes=rounded))


def _resolve_video_time(
    metadata: dict[str, str],
    gps: tuple[float, float] | None,
    filename: str | None = None,
) -> datetime | None:
    """Resolve a video's ``date_taken`` as local wall-clock.

    Returns a timezone-aware datetime whenever a UTC ``creation_time`` is present
    (its timezone is known even when the local offset is not), and a naive
    datetime when only a filename timestamp is available. Downstream indexing
    (OEC-18) derives the naive-local ``date_taken``, the UTC instant, and the
    offset from it. Returns None only when no timestamp exists at all.

    Precedence:
      1. Apple ``com.apple.quicktime.creationdate`` — already local + offset.
      2. UTC ``creation_time`` + explicit vendor offset tag (e.g. Samsung/Android).
      3. UTC ``creation_time`` + GPS location — offset derived from coordinates.
      4. UTC ``creation_time`` + timestamped filename — offset derived from the
         local wall-clock in the name vs the UTC instant (see
         :func:`_offset_from_filename`).
      5. UTC ``creation_time`` alone — kept as tz-aware UTC (local offset unknown,
         so the derived naive-local date_taken may be off by the true offset).
      6. No ``creation_time`` at all (e.g. re-encodes that stripped it) but a
         timestamped filename — naive local wall-clock, offset unknown. Mirrors
         the photo case with no EXIF offset. Falls back to a date-only filename
         (midnight local) when the name carries a date but no time.
    """
    # 1. Apple's tag carries local wall-clock with an explicit offset.
    apple = _parse_creation_time(_container_tag(metadata, "com.apple.quicktime.creationdate"))
    if apple is not None and apple.utcoffset() is not None:
        return apple

    creation = _parse_creation_time(_container_tag(metadata, "creation_time"))
    if creation is None:
        # 6. No UTC anchor: fall back to a naive Apple date, else the filename's
        # local wall-clock, else a date-only filename (midnight), else nothing.
        if apple is not None:
            return apple
        return datetime_from_filename(filename) or date_from_filename(filename)

    # creation_time is UTC by spec; make that explicit before converting.
    utc_dt = creation if creation.tzinfo is not None else creation.replace(tzinfo=UTC)

    # 2. Android vendors record the capture offset in a container tag even when
    # no Apple creationdate or GPS location is present.
    offset = _parse_utc_offset(
        _container_tag(metadata, "com.samsung.android.utc_offset", "com.android.utc_offset")
    )
    if offset is not None:
        return utc_dt.astimezone(offset)

    # 3. Derive the offset from the capture location.
    if gps is not None:
        zone = _timezone_for(gps)
        if zone is not None:
            return utc_dt.astimezone(zone)

    # 4. No tag or location, but many camera apps name clips with the local
    # wall-clock while creation_time is UTC — recover the offset from that gap.
    fname_offset = _offset_from_filename(filename, utc_dt)
    if fname_offset is not None:
        return utc_dt.astimezone(fname_offset)

    # 5. Local offset unknown, but creation_time is UTC by spec — that timezone is
    # itself known, so keep it (tz-aware UTC) rather than discarding it. Downstream
    # (OEC-18) records the exact UTC instant in date_taken_utc; the naive-local
    # date_taken it derives is the UTC wall-clock, which can be off by the true
    # local offset. Unlike a photo's EXIF DateTimeOriginal (genuinely naive local,
    # no tz), a video always carries at least the UTC anchor.
    return utc_dt


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
        """Decode a single representative cover frame as an upright PIL image.

        Seeks ~10% into the clip and decodes one frame — the only frame ever
        decoded (no full-video decode, no audio decode). PyAV does not apply the
        container's display-matrix rotation, so it is applied here: the returned
        image is oriented for display, matching what a video player would show.
        """
        if local_path is None:
            raise ValueError("local_path is required")
        image = self._decode_cover_frame(local_path)
        rotation = _display_rotation(_read_moov_atom(local_path))
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

        # Store display-oriented dimensions: a 90°/270° rotated video is stored
        # landscape but displays portrait, so swap to match the cover frame the
        # gallery renders and uses for aspect ratio.
        if _display_rotation(header) in (90, 270) and width is not None and height is not None:
            width, height = height, width

        # Identity hashes the raw (unrotated) decoded frame, so it stays stable
        # regardless of the rotation-correction logic above.
        self._content_hash = video_identity_hash(header, cover)

        gps = _parse_iso6709(
            _container_tag(metadata, "com.apple.quicktime.location.ISO6709", "location")
        )
        # creation_time is UTC; resolve local wall-clock (offset from the Apple tag,
        # GPS, or a timestamped filename) so date_taken matches the naive-local
        # convention (OEC-18).
        filename = self.path.replace("\\", "/").rsplit("/", 1)[-1]
        date_taken = _resolve_video_time(metadata, gps, filename=filename)
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
