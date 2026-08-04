"""Manifest store for reading and writing the root summary.json."""

from __future__ import annotations

import json
import logging

from .backend import Backend, VersionToken
from .schema import (
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

    async def write_full_summary(self, summary: RootSummary) -> None:
        """Write the thin root summary once, overwriting any existing file.

        Intended to be called exactly once per full indexing session (not per
        partition), so no optimistic-concurrency retry loop is needed here —
        a single writer owns the whole session.
        """
        async with self.backend.partition_lock(""):
            try:
                _, version = await self.read_summary()
                await self.write_summary(summary, version)
            except FileNotFoundError:
                await self.create_summary(summary)
