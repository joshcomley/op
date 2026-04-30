"""op-gateway MCP server entrypoint.

Registered in `~/.claude.json` (or per-project `.mcp.json`) like any
other stdio MCP server. Exposes ONE tool to the SDK: `op`. The tool's
description is built from the snapshot at startup; its parameters
schema is the same `{operation, args}` shape regardless of catalog.

Run as:
  python -m op_gateway.server
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from . import catalog, dispatch, paths
from .manifest import LiveManifest, Snapshot, load_live, load_snapshot


def _load_runtime_files() -> tuple[Snapshot, LiveManifest]:
    """Load op.snapshot.json and op.json. Both must exist; missing files
    are an installation error (run `op promote` first)."""
    snap_path = paths.snapshot_path()
    live_path = paths.live_manifest_path()

    if not snap_path.exists():
        raise RuntimeError(
            f"op.snapshot.json not found at {snap_path}. "
            "Run `python -m op_cli.promote` to generate it from op.json."
        )
    if not live_path.exists():
        raise RuntimeError(
            f"op.json not found at {live_path}. "
            "Copy op.json.example to op.json and edit to suit, then run "
            "`python -m op_cli.promote`."
        )
    return load_snapshot(snap_path), load_live(live_path)


def build_mcp() -> FastMCP:
    """Construct the MCP server, register the `op` tool, return ready to
    run. Factored out so tests can introspect the server without binding
    stdio."""
    snapshot, live = _load_runtime_files()
    description = catalog.build_description(snapshot)

    mcp = FastMCP("op")

    # The MCP SDK derives the tool's input schema from the function's
    # parameter type hints. We want EXACTLY two parameters: `operation`
    # (required string) and `args` (optional object). FastMCP also uses
    # the function's docstring as the tool description, so we set it
    # programmatically by docstring assignment.
    def op(
        operation: str = Field(description="Operation name. Meta-ops: list, describe, sync, health, manifest_version. Domain ops: <namespace>.<tool>."),
        args: dict[str, Any] | None = Field(default=None, description="Optional op-specific arguments object."),
    ) -> str:
        result = dispatch.dispatch(operation, args, snapshot, live)
        # MCP tool results are strings; serialise as compact JSON so the
        # agent sees structured data without trailing whitespace.
        return json.dumps(result, separators=(",", ":"))

    op.__doc__ = description
    mcp.tool()(op)
    return mcp


def main() -> None:
    """Entrypoint when invoked as `python -m op_gateway.server`."""
    mcp = build_mcp()
    mcp.run()


if __name__ == "__main__":
    main()
