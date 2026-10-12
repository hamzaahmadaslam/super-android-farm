"""The farm as an MCP server: `sf mcp` (stdio) and `sf mcp --http` (Streamable HTTP).

This package is the transport only. What a model can see and do, the policy and the
operator's approvals live in `superfarm.ai`, and the server here is one consumer of it.

The MCP SDK is an optional dependency (`pip install "superfarm[mcp]"`). Only `server.py`
imports it; everything else in the farm works without it.
"""
from __future__ import annotations

INSTALL_HINT = 'the MCP SDK is not installed. Install it with:  pip install "superfarm[mcp]"'


def sdk_version() -> str | None:
    """Version of the installed MCP SDK, or None when it is not installed."""
    from importlib import metadata
    try:
        return metadata.version("mcp")
    except metadata.PackageNotFoundError:
        return None
