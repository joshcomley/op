"""Tiny MCP stdio server used by tests as a fake backend.

Implements just enough of the MCP protocol for the BackendPool's
needs: initialize, tools/list, tools/call. Exposes two tools:

  echo  — returns whatever's in `args` as a text content block
  fail  — always returns isError=True

Run as `python -m tests.fake_backend_mcp` (no args). For tests it's
spawned via `StdioServerParameters(command="python", args=[__file__])`.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP


mcp = FastMCP("fake-backend")


@mcp.tool()
def echo(message: str = "hello") -> str:
    """Echo the message back."""
    return f"echo: {message}"


@mcp.tool()
def fail(reason: str = "intentional failure") -> str:
    """Always returns isError. Used by tests of error-handling paths."""
    raise RuntimeError(reason)


if __name__ == "__main__":
    mcp.run()
