"""Manifest store for reading and writing manifests with optimistic concurrency."""

from __future__ import annotations

import json
import logging
from typing import Callable

from .backend import Backend

_log = logging.getLogger(__name__)
from .schema import (
    LeafManifest,
    ParentManifest,
    PartitionSummary,
    VersionConflictError,
    VersionToken,
    deserialize_leaf,
    deserialize_parent,
    manifest_path,
    serialize_leaf,
    serialize_parent,
)


class ManifestStore:
    """Store for reading and writing manifest files with optimistic concurrency."""

    def __init__(self, backend: Backend) -> None:
        """Initialize the manifest store.

        Args:
            backend: Backend instance for storage operations.
        """
        self.backend = backend

    # -----------------------------------------------------------------------
    # Leaf manifests
    # -----------------------------------------------------------------------

    async def read_leaf(self, partition: str) -> tuple[LeafManifest, VersionToken]:
        """Read a leaf manifest and its version token.

        Args:
            partition: Partition path (e.g., "2024/2024-07/").

        Returns:
            Tuple of (LeafManifest, VersionToken).

        Raises:
            FileNotFoundError: If the manifest does not exist.
        """
        path = manifest_path(partition)
        data, version = await self.backend.read(path)
        manifest = deserialize_leaf(json.loads(data.decode("utf-8")))
        return manifest, version

    async def write_leaf(
        self, manifest: LeafManifest, expected_version: VersionToken
    ) -> VersionToken:
        """Write a leaf manifest with optimistic concurrency check.

        Args:
            manifest: LeafManifest to write.
            expected_version: Expected version token (from read_leaf).

        Returns:
            New version token after successful write.

        Raises:
            VersionConflictError: If the manifest was modified since read.
        """
        path = manifest_path(manifest.partition)
        data = json.dumps(serialize_leaf(manifest), ensure_ascii=False, indent=2).encode("utf-8")
        return await self.backend.write_conditional(path, data, expected_version)

    async def create_leaf(self, manifest: LeafManifest) -> VersionToken:
        """Create a new leaf manifest (fails if it already exists).

        Args:
            manifest: LeafManifest to create.

        Returns:
            Version token of the newly created manifest.

        Raises:
            FileExistsError: If the manifest already exists.
        """
        path = manifest_path(manifest.partition)
        data = json.dumps(serialize_leaf(manifest), ensure_ascii=False, indent=2).encode("utf-8")
        return await self.backend.write_new(path, data)

    async def read_modify_write_leaf(
        self,
        partition: str,
        modify: Callable[[LeafManifest], LeafManifest],
        max_retries: int = 3,
    ) -> LeafManifest:
        """Read, modify, and write a leaf manifest with retry on version conflict.

        Args:
            partition: Partition path.
            modify: Function that takes a LeafManifest and returns the modified version.
            max_retries: Maximum number of retries on version conflict.

        Returns:
            The successfully written LeafManifest.

        Raises:
            VersionConflictError: If retries are exhausted.
            FileNotFoundError: If the manifest does not exist.
        """
        for attempt in range(max_retries + 1):
            manifest, version = await self.read_leaf(partition)
            updated = modify(manifest)
            try:
                await self.write_leaf(updated, version)
                return updated
            except VersionConflictError:
                _log.debug(
                    "Version conflict on leaf manifest %r (attempt %d/%d), retrying",
                    partition, attempt + 1, max_retries,
                )
                if attempt == max_retries:
                    raise
                # Re-read and retry

        # Unreachable, but makes type checker happy
        raise RuntimeError("Unexpected control flow")

    # -----------------------------------------------------------------------
    # Parent manifests
    # -----------------------------------------------------------------------

    async def read_parent(self, path: str) -> tuple[ParentManifest, VersionToken]:
        """Read a parent manifest and its version token.

        Args:
            path: Parent manifest path (e.g., "2024/" or "" for root).

        Returns:
            Tuple of (ParentManifest, VersionToken).

        Raises:
            FileNotFoundError: If the manifest does not exist.
        """
        manifest_file = manifest_path(path)
        try:
            data, version = await self.backend.read(manifest_file)
            manifest = deserialize_parent(json.loads(data.decode("utf-8")))
            return manifest, version
        except:
            _log.error(f"Error while deserializing parent manifest of '{path}'")
            raise
        

    async def write_parent(
        self, manifest: ParentManifest, expected_version: VersionToken
    ) -> VersionToken:
        """Write a parent manifest with optimistic concurrency check.

        Args:
            manifest: ParentManifest to write.
            expected_version: Expected version token.

        Returns:
            New version token after successful write.

        Raises:
            VersionConflictError: If the manifest was modified since read.
        """
        path = manifest_path(manifest.path)
        data = json.dumps(serialize_parent(manifest), ensure_ascii=False, indent=2).encode("utf-8")
        return await self.backend.write_conditional(path, data, expected_version)

    async def create_parent(self, manifest: ParentManifest) -> VersionToken:
        """Create a new parent manifest (fails if it already exists).

        Args:
            manifest: ParentManifest to create.

        Returns:
            Version token of the newly created manifest.

        Raises:
            FileExistsError: If the manifest already exists.
        """
        path = manifest_path(manifest.path)
        data = json.dumps(serialize_parent(manifest), ensure_ascii=False, indent=2).encode("utf-8")
        return await self.backend.write_new(path, data)

    async def read_any(
        self, partition: str
    ) -> tuple[LeafManifest | ParentManifest, VersionToken]:
        """Read a manifest at partition, returning whichever type it is.

        Reads the raw JSON and dispatches to LeafManifest or ParentManifest
        based on whether the 'photos' or 'children' key is present. This
        avoids speculative exception-based dispatch that would double I/O.

        Args:
            partition: Partition path (e.g., "2024/2024-07" or "" for root).

        Returns:
            Tuple of (LeafManifest | ParentManifest, VersionToken).

        Raises:
            FileNotFoundError: If no manifest exists at this partition.
            ValueError: If the JSON has neither 'photos' nor 'children'.
        """
        path = manifest_path(partition)
        data, version = await self.backend.read(path)
        raw = json.loads(data.decode("utf-8"))
        if "photos" in raw:
            return deserialize_leaf(raw), version
        elif "children" in raw:
            return deserialize_parent(raw), version
        else:
            raise ValueError(
                f"Manifest at {path!r} has neither 'photos' nor 'children'"
            )

    async def rebuild_parent(
        self,
        parent_path: str,
        child_summaries: list[PartitionSummary],
    ) -> ParentManifest:
        """Rebuild a parent manifest from child summaries.

        This is a convenience method that consolidates child summaries and writes
        the parent manifest with optimistic concurrency.

        Args:
            parent_path: Path of the parent (e.g., "2024/" or "" for root).
            child_summaries: List of PartitionSummary objects from children.

        Returns:
            The written ParentManifest.

        Raises:
            VersionConflictError: If retries are exhausted.
        """
        # TODO: Implement bloom filter merging and min/max date computation
        from .schema import SCHEMA_VERSION

        manifest = ParentManifest(
            schema_version=SCHEMA_VERSION,
            path=parent_path,
            children=child_summaries,
        )

        # Try to update existing, or create new
        try:
            existing, version = await self.read_parent(parent_path)
            manifest._extra = existing._extra  # Preserve unknown fields
            await self.write_parent(manifest, version)
        except FileNotFoundError:
            await self.create_parent(manifest)

        return manifest
