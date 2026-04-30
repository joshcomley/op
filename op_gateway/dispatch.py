"""Operation dispatch — route an `op({operation, args})` call to the
right handler.

Phase 2: domain ops are now forwarded to the backend pool. Meta-ops
still handle locally. Errors come back as structured `{error, ...}`
dicts (not exceptions) so the agent can self-correct without seeing
internal stack traces.
"""
from __future__ import annotations

import time
from typing import Any

from . import events, meta_ops
from .backend_pool import BackendPool, BackendUnavailable
from .manifest import LiveManifest, Snapshot


async def dispatch(
    operation: str,
    args: dict[str, Any] | None,
    snapshot: Snapshot,
    live: LiveManifest,
    pool: BackendPool | None,
) -> dict[str, Any]:
    """Route a single op call. Returns the JSON-serialisable response
    that op-gateway's MCP layer hands back to the SDK as the tool result.

    Errors come back as `{"error": "...", ...}` dicts rather than
    exceptions, because the MCP tool-call protocol expects an in-band
    error structure for the agent to read and self-correct.

    `pool` is None when the gateway is constructed without backend
    wiring (Phase 1 tests). In that mode, domain ops return a
    deterministic placeholder; meta-ops still work."""
    start = time.perf_counter()
    result = await _dispatch_inner(operation, args, snapshot, live, pool)
    duration_ms = int((time.perf_counter() - start) * 1000)
    is_error = isinstance(result, dict) and "error" in result
    events.emit_dispatch(
        operation   = operation if isinstance(operation, str) else "<invalid>",
        duration_ms = duration_ms,
        is_meta     = isinstance(operation, str) and "." not in operation,
        namespace   = (operation.split(".", 1)[0]
                       if isinstance(operation, str) and "." in operation
                       else None),
        success     = not is_error,
        error       = result.get("error") if is_error and isinstance(result, dict) else None,
    )
    return result


async def _dispatch_inner(
    operation: str,
    args: dict[str, Any] | None,
    snapshot: Snapshot,
    live: LiveManifest,
    pool: BackendPool | None,
) -> dict[str, Any]:
    """The actual dispatch logic. Wrapped by `dispatch` for telemetry."""
    if not isinstance(operation, str) or not operation:
        return {
            "error": "missing or invalid 'operation' parameter",
            "expected": {"operation": "<name>", "args": "<object?>"},
        }

    if meta_ops.is_meta_op(operation):
        return meta_ops.dispatch_meta(operation, args, snapshot, live, pool)

    if "." not in operation:
        return {
            "error": f"unknown op: {operation!r}",
            "hint": "Meta-ops are unprefixed (list, describe, sync, health, "
                    "manifest_version). Domain ops are dot-prefixed "
                    "(<namespace>.<tool>). Call op({operation: \"list\"}) "
                    "for the full catalog.",
        }

    namespace, _, tool_name = operation.partition(".")

    # The live registry is the source of truth for what backends + ops
    # exist RIGHT NOW. The snapshot may not list a freshly-added op,
    # but if it's in op.json (live) AND in the backend's `tools/list`,
    # it's callable.
    backend = live.backend_by_name(namespace)
    if backend is None:
        return {
            "error": f"unknown namespace {namespace!r}",
            "hint": "Call op({operation: \"sync\"}) for current state, or "
                    "op({operation: \"list\"}) to see all available namespaces.",
        }

    # Verify the tool name is one the backend exposes. Two sources of
    # truth:
    #   1. The live op.json declares an op with this name under this
    #      backend (manifest-side check) — works without a pool
    #   2. The backend's actual `tools/list` includes a tool with this
    #      name (runtime check, via the pool's cached catalog) — only
    #      consulted when a pool is wired
    #
    # If the manifest claims a tool but the backend doesn't expose it,
    # that's an op.json drift the user should know about — but we still
    # try the call; the backend will return a clean "no such tool"
    # error if appropriate. Conversely, if op.json doesn't list a tool
    # but the backend DOES expose it, we go ahead and call — the manifest
    # is a hint, not a gate.
    declared_op = next((o for o in backend.ops if o.name == tool_name), None)
    in_live_catalog = pool is not None and pool.find_tool(namespace, tool_name) is not None
    if declared_op is None and not in_live_catalog:
        return {
            "error": f"backend {namespace!r} has no op named {tool_name!r}",
            "hint": "Call op({operation: \"sync\"}) to refresh the catalog, "
                    "or op({operation: \"describe\", args: {operation: "
                    f"\"{namespace}.<tool>\"}}) once you know the right name.",
        }

    if pool is None:
        # No pool wired — Phase 1 standalone or test scaffolding.
        # Return the deterministic placeholder so callers can verify
        # the routing reached the right backend.
        return {
            "error": "backend dispatch not implemented (pool not wired)",
            "phase": 1,
            "operation": operation,
            "would_dispatch_to": {
                "backend": backend.name,
                "command": list(backend.command),
                "tool":    tool_name,
                "args":    args or {},
            },
        }

    try:
        return await pool.call_tool(namespace, tool_name, args)
    except BackendUnavailable as exc:
        conn = pool.get(namespace)
        status = conn.status if conn else None
        return {
            "error": f"backend {namespace!r} unavailable",
            "detail": str(exc),
            **({"backend_status": status.to_dict()} if status else {}),
            "hint": "Call op({operation: \"health\"}) for current backend "
                    "status; the gateway's supervisor is auto-reconnecting.",
        }
    except Exception as exc:
        return {
            "error": f"backend dispatch failed: {type(exc).__name__}: {exc}",
            "hint": "This is an unexpected error from the backend. Check "
                    "op({operation: \"health\"}) — the supervisor may have "
                    "torn the connection down to recover.",
        }
