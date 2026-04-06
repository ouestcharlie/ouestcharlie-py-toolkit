"""Progress reporting helpers for MCP agents."""

from __future__ import annotations

import logging

from mcp.server.fastmcp import Context

_log = logging.getLogger(__name__)


async def report_progress(
    ctx: Context,  # type: ignore[type-arg]
    progress: int,
    total: int,
    message: str = "",
) -> None:
    """Safely send a progress notification to the MCP client.

    Swallows any exception with a DEBUG log — the client may disconnect
    or time out while a long-running tool is still running.

    Args:
        ctx: MCP context for the current tool call.
        progress: Current progress value.
        total: Total expected value.
        message: Optional human-readable status message.
    """
    try:
        await ctx.report_progress(progress=progress, total=total, message=message)
    except Exception as exc:
        _log.debug("Progress notification failed (client may have disconnected): %s", exc)
