"""Progress reporting helpers for MCP agents."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.session import ServerSession
    from mcp.server.fastmcp import Context


class ProgressReporter:
    """Rate-limited progress reporter that wraps MCP Context.report_progress.

    Sends progress notifications at most once every 500ms to avoid flooding
    the transport.
    """

    def __init__(
        self,
        ctx: Context[ServerSession, None],
        total: int,
        initial: int = 0,
    ) -> None:
        """Initialize a progress reporter.

        Args:
            ctx: MCP context for sending progress notifications.
            total: Total number of items to process.
            initial: Initial progress value (default: 0).
        """
        self.ctx = ctx
        self.total = total
        self.current = initial
        self._last_sent_time = 0.0
        self._pending_message: str | None = None
        self._min_interval_ms = 500

    async def advance(self, n: int = 1, message: str | None = None) -> None:
        """Advance progress by n items and optionally send a progress notification.

        Progress notifications are rate-limited to avoid flooding the transport.
        If called more frequently than the minimum interval, the latest message
        is queued and sent on the next tick.

        Args:
            n: Number of items to advance (default: 1).
            message: Optional progress message to display.
        """
        self.current += n
        self._pending_message = message

        now = time.time() * 1000  # milliseconds
        if now - self._last_sent_time >= self._min_interval_ms:
            await self._send_progress()

    async def _send_progress(self) -> None:
        """Send the current progress to the MCP context."""
        await self.ctx.report_progress(
            progress=self.current / self.total if self.total > 0 else 0.0,
            total=1.0,
            message=self._pending_message or "",
        )
        self._last_sent_time = time.time() * 1000
        self._pending_message = None

    async def finish(self, message: str | None = None) -> None:
        """Mark progress as complete and send a final notification.

        Args:
            message: Optional completion message.
        """
        self.current = self.total
        self._pending_message = message
        await self._send_progress()
