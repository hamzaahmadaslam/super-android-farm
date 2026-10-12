"""The configuration snippet an MCP client needs to reach this farm.

Most MCP clients read a JSON file with an `mcpServers` object: a name mapped to the command
to launch (stdio) or the URL to connect to (HTTP). The snippet is built from this install's
own paths so it can be pasted as it is. It never contains the API token.
"""
from __future__ import annotations

import sys
from pathlib import Path

from ..config import Config
from ..paths import Paths

SERVER_NAME = "superfarm"
TOKEN_PLACEHOLDER = "<the token printed by: sf token>"      # noqa: S105 - a placeholder, not a token


def command(paths: Paths) -> list[str]:
    """How to start `sf mcp` for this farm home from anywhere: absolute paths only, because
    a client launches it with its own working directory and environment."""
    script = Path(sys.executable).with_name("sf")
    base = [str(script)] if script.is_file() else [sys.executable, "-m", "superfarm.cli.main"]
    return [*base, "--home", str(paths.home.expanduser().absolute()), "mcp"]


def stdio(paths: Paths) -> dict:
    argv = command(paths)
    return {"mcpServers": {SERVER_NAME: {"command": argv[0], "args": argv[1:]}}}


def http(config: Config, port: int | None = None) -> dict:
    url = f"http://127.0.0.1:{port or config.ai.mcp.http_port}/mcp"
    return {"mcpServers": {SERVER_NAME: {"url": url, "headers": {"Authorization": f"Bearer {TOKEN_PLACEHOLDER}"}}}}


def both(config: Config, paths: Paths) -> dict:
    return {"stdio": stdio(paths), "http": http(config),
            "http_start": " ".join([*command(paths), "--http"])}
