"""XMP sidecar store: read and write sidecars with optimistic concurrency."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Iterable
from pathlib import Path

from ouestcharlie_toolkit.media.photo import Photo
from ouestcharlie_toolkit.media.suffixes import VIDEO_SUFFIXES
from ouestcharlie_toolkit.media.video import Video
from ouestcharlie_toolkit.model.entries import XmpSidecar
from ouestcharlie_toolkit.model.tags import DEFAULT_EXCLUDED_TAG_PREFIXES
from ouestcharlie_toolkit.storage.backend import Backend, VersionConflictError, VersionToken
from ouestcharlie_toolkit.xmp.parse import parse_xmp, split_excluded
from ouestcharlie_toolkit.xmp.paths import sidecar_candidates, xmp_path_for
from ouestcharlie_toolkit.xmp.serialize import serialize_xmp

_log = logging.getLogger(__name__)


async def _recover_date_offset(sidecar: XmpSidecar, photo: Photo) -> None:
    """Give a naive sidecar date the UTC offset found in the photo's EXIF.

    darktable rewrites ``exif:DateTimeOriginal`` without its offset. The offset
    is taken from EXIF only when the EXIF local date-time equals the sidecar's
    to the second; otherwise the sidecar's date (maybe corrected by the user)
    stays naive. Never written back: it is recovered again at each index, and
    darktable would strip it at its next sync anyway.
    """
    date = sidecar.date_taken
    if date is None or date.tzinfo is not None:
        return
    try:
        exif_date = await photo.exif_datetime()
    except Exception:
        # Best effort: an unreadable photo keeps the naive sidecar date.
        _log.debug("Could not read EXIF date of %r", photo.path, exc_info=True)
        return
    if exif_date is None or exif_date.tzinfo is None:
        return
    if exif_date.replace(tzinfo=None, microsecond=0) == date.replace(microsecond=0):
        sidecar.date_taken = date.replace(tzinfo=exif_date.tzinfo)


class XmpStore:
    """Store for reading and writing XMP sidecar files with optimistic concurrency."""

    def __init__(
        self,
        backend: Backend,
        excluded_tag_prefixes: Iterable[str] = DEFAULT_EXCLUDED_TAG_PREFIXES,
    ) -> None:
        """Initialize the XMP store.

        Args:
            backend: Backend instance for storage operations.
            excluded_tag_prefixes: Tag paths kept out of ``XmpSidecar.tags`` on
                read (see ``parse_xmp``).
        """
        self.backend = backend
        self.excluded_tag_prefixes = tuple(excluded_tag_prefixes)

    async def _resolve_existing_xmp_path(self, photo_path: str) -> str | None:
        """Find the on-disk sidecar path for a photo, preferring the full-extension form.

        Checks the full-extension convention first (``IMG_001.cr3.xmp``), then
        falls back to the extension-stripped convention (``IMG_001.xmp``).
        Returns None if neither exists.
        """
        for candidate in sidecar_candidates(photo_path):
            if await self.backend.exists(candidate):
                return candidate
        return None

    async def read(self, photo_path: str) -> tuple[XmpSidecar, VersionToken]:
        """Read an XMP sidecar and its version token.

        Args:
            photo_path: Path to the photo file.

        Returns:
            Tuple of (XmpSidecar, VersionToken).

        Raises:
            FileNotFoundError: If the XMP sidecar does not exist.
        """
        xmp_path = await self._resolve_existing_xmp_path(photo_path)
        if xmp_path is None:
            raise FileNotFoundError(f"No XMP sidecar found for {photo_path!r}")
        data, version = await self.backend.read(xmp_path)
        sidecar = parse_xmp(data.decode("utf-8-sig"), self.excluded_tag_prefixes)
        if not sidecar.content_hash:
            _log.warning(f"Empty identity for sidecar '{xmp_path}")
        return sidecar, version

    async def write(
        self,
        photo_path: str,
        sidecar: XmpSidecar,
        expected_version: VersionToken,
    ) -> VersionToken:
        """Write an XMP sidecar with optimistic concurrency check.

        Automatically increments sidecar.metadata_version before writing.
        Callers must hold ``Backend.partition_lock()`` for this photo's
        partition before calling this method.

        Args:
            photo_path: Path to the photo file.
            sidecar: XmpSidecar to write.
            expected_version: Expected version token.

        Returns:
            New version token after successful write.

        Raises:
            VersionConflictError: If the sidecar was modified since read.
        """
        xmp_path = await self._resolve_existing_xmp_path(photo_path)
        if xmp_path is None:
            xmp_path = xmp_path_for(photo_path, with_photo_extension=True)
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
        xmp_path = xmp_path_for(photo_path, with_photo_extension=True)
        xml = serialize_xmp(sidecar)
        return await self.backend.write_new(xmp_path, xml.encode("utf-8"))

    async def read_or_create_from_picture(
        self,
        photo_path: str,
        force: bool = False,
    ) -> tuple[XmpSidecar, VersionToken, bool]:
        """Return the XMP sidecar for a photo or video, creating it if needed.

        Dispatches on the file extension: photos (default) go through EXIF
        extraction, videos through container/stream extraction. Both produce an
        :class:`XmpSidecar`, so the sidecar format is uniform across media types.

        If an XMP sidecar already exists and ``force=False``, the existing
        sidecar is returned unchanged.  If the existing sidecar lacks
        ``ouestcharlie:contentHash`` (e.g. a third-party sidecar written by
        Lightroom), the hash is computed from the media file and stored on the
        returned sidecar object without writing back. Likewise, a naive date
        in a photo's sidecar (darktable drops the offset) gets the offset from
        the photo's EXIF when both dates match.

        If no sidecar exists, or ``force=True``, metadata is extracted from the
        media file, a new sidecar is written, and the new sidecar is returned.

        Args:
            photo_path: Path to the photo or video file (relative to backend root).
            force: Re-extract metadata and overwrite any existing sidecar.

        Returns:
            ``(sidecar, version_token, created)`` where ``created`` is ``True``
            when a new sidecar was written to the backend.
        """
        # Select the media handler by extension. Both expose create_identity();
        # extraction differs (extract_exif vs extract_metadata).
        photo: Photo | None = None
        if Path(photo_path).suffix.lower() in VIDEO_SUFFIXES:
            video = Video(self.backend, photo_path)
            extract = video.extract_metadata
            create_identity = video.create_identity
        else:
            photo = Photo(self.backend, photo_path)
            extract = photo.extract_exif
            create_identity = photo.create_identity

        existing_sidecar: XmpSidecar | None = None
        existing_version: VersionToken | None = None
        with contextlib.suppress(FileNotFoundError):
            existing_sidecar, existing_version = await self.read(photo_path)

        if existing_sidecar is not None and not force:
            if not existing_sidecar.content_hash:
                # Third-party sidecar without ouestcharlie:contentHash.
                existing_sidecar.content_hash = await create_identity()
            if photo is not None:
                await _recover_date_offset(existing_sidecar, photo)
            assert existing_version is not None
            return existing_sidecar, existing_version, False

        # Extract metadata and write sidecar (new or forced overwrite).
        sidecar = await extract()
        sidecar.tags, sidecar._excluded_tags = split_excluded(
            sidecar.tags, self.excluded_tag_prefixes
        )
        if existing_version is not None:
            new_version = await self.write(photo_path, sidecar, existing_version)
        else:
            new_version = await self.create(photo_path, sidecar)
        return sidecar, new_version, True

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
                _log.debug(
                    "Version conflict on %r (attempt %d/%d), retrying",
                    photo_path,
                    attempt + 1,
                    max_retries,
                )
                if attempt == max_retries:
                    raise

        raise RuntimeError("Unexpected control flow")
