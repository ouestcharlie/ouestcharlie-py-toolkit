"""ISO-BMFF (MP4/MOV) box parsing: the moov atom and the display rotation."""

from __future__ import annotations

import math
import struct
from collections.abc import Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# Cap on the container-header bytes hashed for identity. The moov sample tables
# grow with clip length but stay far below the media payload; on the rare
# overflow we hash the capped prefix (deterministic, still far stronger than
# scalar metadata). See #39 §3.
_MOOV_READ_CAP = 16 * 1024 * 1024


def read_moov_atom(path: Path) -> bytes:
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


def display_rotation(moov: bytes) -> int:
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
