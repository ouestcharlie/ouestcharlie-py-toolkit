"""Content hashing utilities for OuEstCharlie."""

from __future__ import annotations

import base64

import blake3


def content_hash(data: bytes) -> str:
    """Return a compact content hash for *data*.

    Uses BLAKE3 truncated to 128 bits, base64url-encoded without padding.
    Produces a 22-character URL- and filename-safe string.
    """
    digest = blake3.blake3(data).digest(length=16)
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
