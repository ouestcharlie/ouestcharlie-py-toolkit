"""MCP server base class for OuEstCharlie agents."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession

from .backend import Backend, backend_from_config
from .manifest import ManifestStore
from .schema import ConfigurationError
from .xmp import XmpStore

_log = logging.getLogger(__name__)


class AgentBase:
    """Base class for OuEstCharlie agents that wraps FastMCP.

    Handles:
    - Environment variable parsing (WOOF_BACKEND_CONFIG, WOOF_AGENT_TOKEN)
    - Backend initialization
    - Progress reporting factory
    - Cooperative cancellation
    - Per-photo error isolation
    - Structured logging via MCP
    """

    def __init__(self, name: str, version: str = "1.0.0") -> None:
        """Initialize the agent base.

        Args:
            name: Agent name (e.g., "ouestcharlie-housekeeping").
            version: Agent version.
        """
        self.name = name
        self.version = version
        self.mcp = FastMCP(name=name)
        self._cancelled = False
        self._current_ctx: Context[ServerSession, None] | None = None

        # Parse environment
        self.backend_config = self._parse_backend_config()
        self.agent_token = os.environ.get("WOOF_AGENT_TOKEN", "")

        # Initialize backend
        self.backend: Backend = backend_from_config(self.backend_config)
        self.manifest_store = ManifestStore(self.backend)
        self.xmp_store = XmpStore(self.backend)

    def _parse_backend_config(self) -> dict[str, Any]:
        """Parse WOOF_BACKEND_CONFIG from environment."""
        config_json = os.environ.get("WOOF_BACKEND_CONFIG")
        if not config_json:
            raise ConfigurationError("WOOF_BACKEND_CONFIG environment variable not set")

        try:
            config = json.loads(config_json)
            if not isinstance(config, dict):
                raise ValueError("Config must be a JSON object")
            return config
        except (json.JSONDecodeError, ValueError) as e:
            raise ConfigurationError(f"Invalid WOOF_BACKEND_CONFIG: {e}") from e

    @property
    def cancelled(self) -> bool:
        """Check if the agent has been cancelled."""
        return self._cancelled

    async def check_cancelled(self) -> None:
        """Check if the agent has been cancelled and raise CancelledError if so.

        Agents should call this periodically (e.g., in processing loops) to
        enable cooperative cancellation.

        Raises:
            asyncio.CancelledError: If the agent has been cancelled.
        """
        if self._cancelled:
            raise asyncio.CancelledError("Agent cancelled by Woof")

    @asynccontextmanager
    async def per_photo(self, photo: str, partition: str) -> AsyncIterator[PerPhotoContext]:
        """Context manager for per-photo processing with error isolation.

        Catches exceptions and logs them as permanent/transient errors via MCP
        without aborting the batch. The caller can check ctx.failed after the
        block to tally errors.

        Args:
            photo: Photo filename.
            partition: Partition path.

        Yields:
            PerPhotoContext with a `failed` flag.

        Example:
            async for photo in photos:
                async with self.per_photo(photo, partition) as ctx:
                    await process_photo(photo)
                if ctx.failed:
                    error_count += 1
        """
        ctx = PerPhotoContext(photo=photo, partition=partition)
        try:
            yield ctx
        except FileNotFoundError as e:
            # Permanent error: photo file missing
            _log.error(
                "Photo file not found — partition=%r photo=%r: %s",
                partition,
                photo,
                e,
                exc_info=True,
            )
            await self._log_error(
                "permanent",
                f"Photo file not found: {e}",
                photo=photo,
                partition=partition,
                operation="photo read",
            )
            ctx.failed = True
        except Exception as e:
            # Permanent error by default (agents can re-raise transient errors differently)
            _log.error(
                "Failed to process photo — partition=%r photo=%r: %s",
                partition,
                photo,
                e,
                exc_info=True,
            )
            await self._log_error(
                "permanent",
                f"Failed to process photo: {e}",
                photo=photo,
                partition=partition,
                operation="photo processing",
            )
            ctx.failed = True

    async def _log_error(
        self,
        category: str,
        message: str,
        photo: str,
        partition: str,
        operation: str,
    ) -> None:
        """Log a structured error via MCP notifications/message.

        Args:
            category: Error category ("transient", "permanent", "configuration").
            message: Error message.
            photo: Photo filename.
            partition: Partition path.
            operation: Operation that failed.
        """
        if self._current_ctx is not None:
            # Use MCP context logging
            # FastMCP context doesn't expose a direct error() method,
            # so we'd need to use the session's notification mechanism
            # For now, this is a placeholder
            # TODO: Send notifications/message via self._current_ctx
            pass

    def run(self) -> None:
        """Run the MCP server on stdio transport."""
        self.mcp.run()


class PerPhotoContext:
    """Context object yielded by per_photo() context manager."""

    def __init__(self, photo: str, partition: str) -> None:
        self.photo = photo
        self.partition = partition
        self.failed = False
