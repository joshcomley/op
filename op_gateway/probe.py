"""Connect to backend MCP servers, harvest their tools/list, return
schema hashes.

Used by `op promote` to enrich the snapshot with each tool's schema
hash, so subsequent `op({operation: "sync"})` calls can detect when a
backend has changed a tool's argument shape since the snapshot was
taken.

Reuses the BackendPool machinery rather than reinventing the MCP
client. Connections are spawned, polled until reachable, queried, and
torn down — there's no need to keep them alive past the probe.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from .backend_pool import BackendPool
from .manifest import BackendDef, canonical_schema_hash


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProbedTool:
    """One tool's data as harvested from a backend's tools/list."""
    backend:     str        # backend name (the namespace)
    tool_name:   str        # backend-side name, e.g. "recap_all"
    description: str        # the backend's full description text
    schema:      Any        # the inputSchema dict (passed through verbatim)
    schema_hash: str        # canonical_schema_hash(schema)


async def probe_backends(
    backends: list[BackendDef],
    *,
    timeout_secs: float = 10.0,
) -> dict[str, list[ProbedTool]]:
    """Spawn each backend, fetch its tools/list, return per-backend
    probed tools. Tears down all connections before returning.

    Backends that fail to connect within `timeout_secs` get an empty
    list in the returned dict — `op promote` then writes their snapshot
    entries without a schema_hash, and `sync` falls back to name-only
    diff for those ops.
    """
    if not backends:
        return {}

    pool = BackendPool(list(backends))
    try:
        await pool.start_all()
        connect_results = await pool.wait_for_initial_connect(timeout_secs=timeout_secs)
        out: dict[str, list[ProbedTool]] = {}
        for backend in backends:
            up = connect_results.get(backend.name, False)
            conn = pool.get(backend.name)
            if not up or conn is None:
                log.warning(
                    "op probe: backend %r did not reach `up` within %.1fs; "
                    "snapshot will skip schema_hash for its ops "
                    "(last_error: %s)",
                    backend.name, timeout_secs,
                    conn.status.last_error if conn else "(no connection)",
                )
                out[backend.name] = []
                continue
            tools: list[ProbedTool] = []
            for t in conn.tools:
                schema = getattr(t, "inputSchema", None)
                tools.append(ProbedTool(
                    backend=backend.name,
                    tool_name=getattr(t, "name", ""),
                    description=getattr(t, "description", "") or "",
                    schema=schema,
                    schema_hash=canonical_schema_hash(schema),
                ))
            out[backend.name] = tools
        return out
    finally:
        await pool.stop_all()


def probe_backends_sync(
    backends: list[BackendDef],
    *,
    timeout_secs: float = 10.0,
) -> dict[str, list[ProbedTool]]:
    """Sync wrapper around `probe_backends` for the `op promote` CLI,
    which doesn't run inside an event loop.

    Uses asyncio.run so we get a fresh loop scoped to the probe. Safe
    on every platform; the alternative (asyncio.get_event_loop) is
    deprecated in 3.12+."""
    return asyncio.run(probe_backends(backends, timeout_secs=timeout_secs))


def hash_lookup(probed: dict[str, list[ProbedTool]]) -> dict[tuple[str, str], str]:
    """Flatten probed tools into a (backend, tool_name) -> schema_hash
    map. Used by both promote (writing snapshot entries) and diff
    (comparing snapshot vs live)."""
    out: dict[tuple[str, str], str] = {}
    for backend_name, tools in probed.items():
        for t in tools:
            out[(backend_name, t.tool_name)] = t.schema_hash
    return out
