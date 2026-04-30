"""op-gateway MCP server entrypoint.

Registered in `~/.claude.json` (or per-project `.mcp.json`) like any
other stdio MCP server. Exposes ONE tool to the SDK: `op`. The tool's
description is built from the snapshot at startup; its parameters
schema is `{operation, args}` regardless of catalog.

Run as:
  python -m op_gateway.server
"""
from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from . import catalog, dispatch, paths
from .backend_pool import BackendPool
from .manifest import LiveManifest, Snapshot, load_live, load_snapshot


log = logging.getLogger(__name__)


# Disable backend wiring entirely. Useful for tests + the pre-Phase-2
# stand-alone mode where the gateway only serves meta-ops.
_DISABLE_POOL_ENV = "OP_DISABLE_POOL"


def _load_runtime_files() -> tuple[Snapshot, LiveManifest]:
    """Load op.snapshot.json and op.json. Both must exist; missing files
    are an installation error (run `op promote` first)."""
    snap_path = paths.snapshot_path()
    live_path = paths.live_manifest_path()

    if not snap_path.exists():
        raise RuntimeError(
            f"op.snapshot.json not found at {snap_path}. "
            "Run `python -m op_cli promote` to generate it from op.json."
        )
    if not live_path.exists():
        raise RuntimeError(
            f"op.json not found at {live_path}. "
            "Copy op.json.example to op.json and edit to suit, then run "
            "`python -m op_cli promote`."
        )
    return load_snapshot(snap_path), load_live(live_path)


def build_mcp() -> FastMCP:
    """Construct the MCP server, register the `op` tool, return ready to
    run.

    The pool is owned by the FastMCP server's lifespan. It starts when
    the SDK opens the stdio (gateway process spawns) and stops when the
    SDK closes it (gateway process exits). Tool handlers reach the pool
    through a closure variable set during lifespan setup.
    """
    snapshot, live = _load_runtime_files()
    description = catalog.build_description(snapshot)

    # Pool reference held in a closure-mutable container so the tool
    # handler can pick it up after lifespan-setup. None means "no pool"
    # — domain ops return a deterministic placeholder in that mode.
    state: dict[str, BackendPool | None] = {"pool": None}

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[dict[str, Any]]:
        if os.environ.get(_DISABLE_POOL_ENV):
            log.info("op-gateway: pool disabled by %s; meta-ops only.", _DISABLE_POOL_ENV)
            yield {}
            return
        if not live.backends:
            log.info("op-gateway: no backends declared in op.json; meta-ops only.")
            yield {}
            return
        pool = BackendPool(list(live.backends))
        try:
            await pool.start_all()
            state["pool"] = pool
            log.info(
                "op-gateway: backend pool started (%d backends): %s",
                len(live.backends), ", ".join(b.name for b in live.backends),
            )
            yield {"pool": pool}
        finally:
            state["pool"] = None
            await pool.stop_all()

    mcp = FastMCP("op", lifespan=lifespan)

    async def op(
        operation: str = Field(
            description="Operation name. Meta-ops: list, describe, sync, "
                        "health, manifest_version. Domain ops: <namespace>.<tool>."
        ),
        args: dict[str, Any] | None = Field(
            default=None,
            description="Optional op-specific arguments object.",
        ),
    ) -> str:
        result = await dispatch.dispatch(
            operation, args, snapshot, live, state.get("pool"),
        )
        return json.dumps(result, separators=(",", ":"))

    op.__doc__ = description
    mcp.tool()(op)
    return mcp


def main() -> None:
    """Entrypoint when invoked as `python -m op_gateway.server`."""
    logging.basicConfig(
        level=os.environ.get("OP_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    mcp = build_mcp()
    mcp.run()


if __name__ == "__main__":
    main()
