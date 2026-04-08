"""Manifest store for reading and writing manifests with optimistic concurrency."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

from .backend import Backend, VersionConflictError, VersionToken
from .schema import (
    METADATA_DIR,
    SCHEMA_VERSION,
    LeafManifest,
    ManifestSummary,
    RootSummary,
    deserialize_leaf,
    deserialize_summary,
    manifest_path,
    serialize_leaf,
    serialize_summary,
    summary_path,
)

_log = logging.getLogger(__name__)


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
        return await self.backend.write_conditional(
            path, data, expected_version, path.rsplit("/", 1)[0]
        )

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
                    partition,
                    attempt + 1,
                    max_retries,
                )
                if attempt == max_retries:
                    raise
                # Re-read and retry

        # Unreachable, but makes type checker happy
        raise RuntimeError("Unexpected control flow")

    # -----------------------------------------------------------------------
    # Root summary (summary.json)
    # -----------------------------------------------------------------------

    async def read_summary(self) -> tuple[RootSummary, VersionToken]:
        """Read the root summary and its version token.

        Returns:
            Tuple of (RootSummary, VersionToken).

        Raises:
            FileNotFoundError: If summary.json does not exist yet.
        """
        path = summary_path()
        data, version = await self.backend.read(path)
        return deserialize_summary(json.loads(data.decode("utf-8"))), version

    async def write_summary(
        self, summary: RootSummary, expected_version: VersionToken
    ) -> VersionToken:
        """Write the root summary with optimistic concurrency check.

        Raises:
            VersionConflictError: If the file was modified since read.
        """
        path = summary_path()
        data = json.dumps(serialize_summary(summary), ensure_ascii=False, indent=2).encode("utf-8")
        return await self.backend.write_conditional(path, data, expected_version, METADATA_DIR)

    async def create_summary(self, summary: RootSummary) -> VersionToken:
        """Create the root summary (fails if it already exists).

        Raises:
            FileExistsError: If summary.json already exists.
        """
        path = summary_path()
        data = json.dumps(serialize_summary(summary), ensure_ascii=False, indent=2).encode("utf-8")
        return await self.backend.write_new(path, data)

    async def upsert_partition_in_summary(
        self,
        new_partition_summary: ManifestSummary,
        max_retries: int = 5,
    ) -> RootSummary:
        """Atomically update (or insert) one partition's entry in summary.json.

        Uses a read-modify-write loop with optimistic concurrency, retrying on
        VersionConflictError. Handles the case where summary.json does not yet
        exist (first index of the backend).

        Args:
            new_partition_summary: The summary to insert or replace.
            max_retries: Maximum retry count on concurrent write conflicts.

        Returns:
            The successfully written RootSummary.
        """
        for attempt in range(max_retries + 1):
            try:
                existing, version = await self.read_summary()
                partitions = [
                    p for p in existing.partitions if p.path != new_partition_summary.path
                ]
                partitions.append(new_partition_summary)
                updated = RootSummary(
                    schema_version=existing.schema_version,
                    partitions=partitions,
                    _extra=existing._extra,
                )
                await self.write_summary(updated, version)
                return updated
            except FileNotFoundError:
                fresh = RootSummary(
                    schema_version=SCHEMA_VERSION,
                    partitions=[new_partition_summary],
                )
                try:
                    await self.create_summary(fresh)
                    return fresh
                except FileExistsError:
                    pass  # Race: another writer created it; retry the read path
            except VersionConflictError:
                _log.debug(
                    "Version conflict updating summary.json (attempt %d/%d), retrying",
                    attempt + 1,
                    max_retries,
                )
                if attempt == max_retries:
                    raise
        raise RuntimeError("Unexpected control flow in upsert_partition_in_summary")
