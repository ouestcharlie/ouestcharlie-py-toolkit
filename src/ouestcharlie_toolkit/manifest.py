"""Manifest store for reading and writing the root summary.json."""

from __future__ import annotations

import json
import logging

from .backend import Backend, VersionConflictError, VersionToken
from .schema import (
    SCHEMA_VERSION,
    ManifestSummary,
    RootSummary,
    deserialize_summary,
    serialize_summary,
    summary_path,
)

_log = logging.getLogger(__name__)


class ManifestStore:
    """Store for reading and writing the root summary with optimistic concurrency."""

    def __init__(self, backend: Backend) -> None:
        """Initialize the manifest store.

        Args:
            backend: Backend instance for storage operations.
        """
        self.backend = backend

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

        Callers must hold ``Backend.partition_lock("")`` before calling
        this method.

        Raises:
            VersionConflictError: If the file was modified since read.
        """
        path = summary_path()
        data = json.dumps(serialize_summary(summary), ensure_ascii=False, indent=2).encode("utf-8")
        return await self.backend.write_conditional(path, data, expected_version)

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

        Empty (photo_count == 0) summaries are deleted

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
                # Do not happened if photo_count == 0
                if new_partition_summary.photo_count > 0:
                    partitions.append(new_partition_summary)
                updated = RootSummary(
                    schema_version=max(
                        existing.schema_version, SCHEMA_VERSION
                    ),  # Updating a partition is always to the current SCHEMA_VERSION
                    partitions=partitions,
                    _extra=existing._extra,
                )
                async with self.backend.partition_lock(""):
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
