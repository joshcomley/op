"""op-gateway MCP server entrypoint.

Registered in `~/.claude.json` (or per-project `.mcp.json`) like any
other stdio MCP server. Exposes ONE tool to the SDK: `op`. The tool's
description is built from the snapshot at startup; its parameters
schema is `{operation, args}` regardless of catalog.

Run as:
  python -m op_gateway.server
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from . import catalog, dispatch, events, paths
from .backend_pool import BackendPool
from .manifest import LiveManifest, Snapshot, load_live, load_snapshot


log = logging.getLogger(__name__)


# Disable backend wiring entirely. Useful for tests + the pre-Phase-2
# stand-alone mode where the gateway only serves meta-ops.
_DISABLE_POOL_ENV = "OP_DISABLE_POOL"

# Disable the hot-reload watcher. The gateway loads op.json once at
# startup and never re-reads it. Edits require a gateway restart
# (e.g. closing + reopening the Claude session).
_DISABLE_WATCHER_ENV = "OP_DISABLE_WATCHER"

# Poll interval for the op.json mtime check.
_RELOAD_POLL_INTERVAL_SECS = 2.0


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
    initial_snapshot, initial_live = _load_runtime_files()
    description = catalog.build_description(initial_snapshot)

    # Mutable state held in a closure-shared container so:
    #   * the tool handler can pick the pool up after lifespan-setup
    #   * the hot-reload watcher can swap `live` in place when op.json
    #     changes (the snapshot stays fixed for the gateway's lifetime;
    #     it changes only via `op promote` + restart)
    # None values for the pool mean "no pool" — domain ops return a
    # deterministic placeholder in that mode.
    state: dict[str, Any] = {
        "pool":     None,
        "live":     initial_live,
        "snapshot": initial_snapshot,
    }

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[dict[str, Any]]:
        # Wire the telemetry sink for the gateway's lifetime. No-op when
        # OP_EVENTS_FILE isn't set — zero overhead in the hot path.
        sink = events.sink_from_env()
        events.set_sink(sink)
        try:
            if os.environ.get(_DISABLE_POOL_ENV):
                log.info(
                    "op-gateway: pool disabled by %s; meta-ops only.",
                    _DISABLE_POOL_ENV,
                )
                yield {}
                return
            if not initial_live.backends:
                log.info("op-gateway: no backends declared in op.json; meta-ops only.")
                yield {}
                return
            pool = BackendPool(list(initial_live.backends))
            watcher_task: asyncio.Task[None] | None = None
            try:
                await pool.start_all()
                state["pool"] = pool
                log.info(
                    "op-gateway: backend pool started (%d backends): %s",
                    len(initial_live.backends),
                    ", ".join(b.name for b in initial_live.backends),
                )
                # Hot-reload watcher: poll op.json's mtime and reconcile
                # the pool when it changes. The agent learns about new
                # ops via `op({operation: "sync"})` — the SDK's cached
                # tool description never changes from this.
                if not os.environ.get(_DISABLE_WATCHER_ENV):
                    watcher_task = asyncio.create_task(
                        _reload_watcher(state, paths.live_manifest_path()),
                        name="op-gateway-reload-watcher",
                    )
                yield {"pool": pool}
            finally:
                if watcher_task is not None:
                    watcher_task.cancel()
                    try:
                        await watcher_task
                    except (asyncio.CancelledError, Exception):
                        pass
                state["pool"] = None
                await pool.stop_all()
        finally:
            sink.close()
            events.set_sink(None)

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
        # Resolve current state at call time. `live` is the only thing
        # that mutates during the gateway's lifetime — via the
        # hot-reload watcher when op.json changes. `snapshot` is fixed
        # (changes only via `op promote` + gateway restart) — that's
        # exactly what keeps the SDK's cached tool description stable.
        result = await dispatch.dispatch(
            operation,
            args,
            state["snapshot"],
            state["live"],
            state.get("pool"),
        )
        return json.dumps(result, separators=(",", ":"))

    op.__doc__ = description
    mcp.tool()(op)
    return mcp


async def _reload_watcher(
    state: dict[str, Any],
    op_json_path: Any,
) -> None:
    """Background task that polls op.json's mtime and reconciles the
    backend pool when it changes.

    Mtime polling beats filesystem-event watching here because:
      * cross-platform (no watchdog dependency)
      * resilient to editor save patterns (some editors write to a temp
        file then atomically rename, which trips fsevents but updates
        mtime cleanly)
      * sub-2s polling is fine for a config file that's edited by hand

    On a parse error, the previous pool state is preserved and an
    error is logged — better than tearing down everyone's connections
    because someone left a trailing comma in op.json. The watcher
    keeps polling and will converge once the file is valid again.
    """
    last_mtime: float | None = None
    try:
        last_mtime = op_json_path.stat().st_mtime
    except OSError:
        # File didn't exist at startup. The lifespan would have failed
        # earlier; this branch is defensive.
        return

    while True:
        await asyncio.sleep(_RELOAD_POLL_INTERVAL_SECS)
        pool = state.get("pool")
        if pool is None:
            # Pool got torn down (lifespan exit). Watcher is about to
            # be cancelled too; just return.
            return
        try:
            current_mtime = op_json_path.stat().st_mtime
        except OSError as e:
            log.warning(
                "op-gateway: reload watcher couldn't stat %s: %s",
                op_json_path, e,
            )
            continue
        if last_mtime is not None and current_mtime == last_mtime:
            continue
        # File changed. Try to load it; on failure, leave the pool
        # alone and keep polling.
        try:
            new_live = load_live(op_json_path)
        except Exception as e:
            log.error(
                "op-gateway: reload of %s failed (%s: %s); keeping previous "
                "pool state. Edit the file again to retry.",
                op_json_path, type(e).__name__, e,
            )
            # Don't update last_mtime — we want to retry the same edit
            # once the user fixes it. (If they make a SECOND edit, the
            # mtime will change again and we'll reload then.)
            continue
        log.info("op-gateway: reload watcher detected op.json change; reconciling.")
        try:
            actions = await pool.reconcile(list(new_live.backends))
            # Swap `live` in place so subsequent dispatch() calls see
            # the new declared ops + backends. The snapshot stays put.
            state["live"] = new_live
            log.info("op-gateway: reconcile actions: %s", actions)
        except Exception as e:
            log.exception(
                "op-gateway: reconcile failed (%s: %s); some backends may "
                "be in an inconsistent state.",
                type(e).__name__, e,
            )
        last_mtime = current_mtime


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
