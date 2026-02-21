"""XMP sidecar store for reading and writing XMP files with optimistic concurrency."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable

from .backend import Backend
from .schema import VersionConflictError, VersionToken, XmpSidecar


def xmp_path_for(photo_path: str) -> str:
    """Compute the XMP sidecar path for a photo file.

    Args:
        photo_path: Path to the photo file (e.g., "2024/2024-07/IMG_001.jpg").

    Returns:
        Path to the XMP sidecar (e.g., "2024/2024-07/IMG_001.xmp").
    """
    p = Path(photo_path)
    return str(p.with_suffix(".xmp"))


class XmpStore:
    """Store for reading and writing XMP sidecar files with optimistic concurrency."""

    def __init__(self, backend: Backend) -> None:
        """Initialize the XMP store.

        Args:
            backend: Backend instance for storage operations.
        """
        self.backend = backend

    async def read(self, photo_path: str) -> tuple[XmpSidecar, VersionToken]:
        """Read an XMP sidecar and its version token.

        Args:
            photo_path: Path to the photo file.

        Returns:
            Tuple of (XmpSidecar, VersionToken).

        Raises:
            FileNotFoundError: If the XMP sidecar does not exist.
        """
        xmp_path = xmp_path_for(photo_path)
        data, version = await self.backend.read(xmp_path)
        sidecar = parse_xmp(data.decode("utf-8"))
        return sidecar, version

    async def write(
        self, photo_path: str, sidecar: XmpSidecar, expected_version: VersionToken
    ) -> VersionToken:
        """Write an XMP sidecar with optimistic concurrency check.

        Automatically increments sidecar.metadata_version before writing.

        Args:
            photo_path: Path to the photo file.
            sidecar: XmpSidecar to write.
            expected_version: Expected version token.

        Returns:
            New version token after successful write.

        Raises:
            VersionConflictError: If the sidecar was modified since read.
        """
        xmp_path = xmp_path_for(photo_path)
        sidecar.metadata_version += 1
        xml = serialize_xmp(sidecar)
        return await self.backend.write_conditional(xmp_path, xml.encode("utf-8"), expected_version)

    async def create(self, photo_path: str, sidecar: XmpSidecar) -> VersionToken:
        """Create a new XMP sidecar (fails if it already exists).

        Args:
            photo_path: Path to the photo file.
            sidecar: XmpSidecar to create.

        Returns:
            Version token of the newly created sidecar.

        Raises:
            FileExistsError: If the sidecar already exists.
        """
        xmp_path = xmp_path_for(photo_path)
        xml = serialize_xmp(sidecar)
        return await self.backend.write_new(xmp_path, xml.encode("utf-8"))

    async def read_modify_write(
        self,
        photo_path: str,
        modify: Callable[[XmpSidecar], XmpSidecar],
        max_retries: int = 3,
    ) -> XmpSidecar:
        """Read, modify, and write an XMP sidecar with retry on version conflict.

        Args:
            photo_path: Path to the photo file.
            modify: Function that takes an XmpSidecar and returns the modified version.
            max_retries: Maximum number of retries on version conflict.

        Returns:
            The successfully written XmpSidecar.

        Raises:
            VersionConflictError: If retries are exhausted.
            FileNotFoundError: If the sidecar does not exist.
        """
        for attempt in range(max_retries + 1):
            sidecar, version = await self.read(photo_path)
            updated = modify(sidecar)
            try:
                await self.write(photo_path, updated, version)
                return updated
            except VersionConflictError:
                if attempt == max_retries:
                    raise
                # Re-read and retry

        # Unreachable, but makes type checker happy
        raise RuntimeError("Unexpected control flow")


# ---------------------------------------------------------------------------
# XMP parsing and serialization (stubs using pyexiv2)
# ---------------------------------------------------------------------------


def parse_xmp(xml: str) -> XmpSidecar:
    """Parse XMP XML into an XmpSidecar dataclass.

    This is a stub implementation. The real implementation will use pyexiv2
    to parse the full XMP structure.

    Args:
        xml: XMP XML string.

    Returns:
        XmpSidecar with extracted fields.
    """
    # TODO: Implement using pyexiv2
    # Example pyexiv2 usage:
    #   import pyexiv2
    #   metadata = pyexiv2.ImageMetadata.from_buffer(xml.encode())
    #   metadata.read()
    #   return XmpSidecar(
    #       content_hash=metadata.get("Xmp.ouestcharlie.contentHash"),
    #       ...
    #   )
    raise NotImplementedError("XMP parsing not yet implemented")


def serialize_xmp(sidecar: XmpSidecar) -> str:
    """Serialize an XmpSidecar to XMP XML.

    This is a stub implementation. The real implementation will use pyexiv2
    to generate proper XMP XML while preserving unknown fields from _raw_xml.

    Args:
        sidecar: XmpSidecar to serialize.

    Returns:
        XMP XML string.
    """
    # TODO: Implement using pyexiv2
    # Need to:
    # 1. Parse _raw_xml to preserve unknown fields/namespaces
    # 2. Update known fields (ouestcharlie:*, exif:*, dc:subject for tags)
    # 3. Increment metadataVersion
    # 4. Serialize back to XML
    raise NotImplementedError("XMP serialization not yet implemented")


# ---------------------------------------------------------------------------
# EXIF extraction and content hashing
# ---------------------------------------------------------------------------


async def extract_exif(backend: Backend, photo_path: str) -> XmpSidecar:
    """Extract EXIF metadata from a photo file and create an XmpSidecar.

    This is a stub implementation. The real implementation will use pyexiv2
    to read EXIF from the photo.

    Args:
        backend: Backend to read the photo file.
        photo_path: Path to the photo file.

    Returns:
        XmpSidecar populated with EXIF fields.
    """
    # TODO: Implement using pyexiv2
    # Example:
    #   data, _ = await backend.read(photo_path)
    #   with tempfile.NamedTemporaryFile(delete=False) as tmp:
    #       tmp.write(data)
    #       tmp.flush()
    #       import pyexiv2
    #       metadata = pyexiv2.ImageMetadata(tmp.name)
    #       metadata.read()
    #       return XmpSidecar(
    #           date_taken=metadata.get("Exif.Photo.DateTimeOriginal"),
    #           camera_make=metadata.get("Exif.Image.Make"),
    #           ...
    #       )
    raise NotImplementedError("EXIF extraction not yet implemented")


async def compute_content_hash(backend: Backend, photo_path: str) -> str:
    """Compute SHA-256 content hash of a photo file.

    Args:
        backend: Backend to read the photo file.
        photo_path: Path to the photo file.

    Returns:
        Content hash string in the format "sha256:hexdigest".
    """
    data, _ = await backend.read(photo_path)
    digest = hashlib.sha256(data).hexdigest()
    return f"sha256:{digest}"
