"""Meta-op handlers — list, describe, sync, health, manifest_version.

`describe` and `health` use live backend data when the pool is wired up
(Phase 2+); fall back to snapshot data when there's no pool (tests, or
the gateway started without backends).
"""
from __future__ import annotations

from typing import Any

from .backend_pool import BackendPool, BackendStatus
from .diff import diff
from .manifest import LiveManifest, Snapshot


META_OP_NAMES = frozenset({
    "list",
    "describe",
    "sync",
    "health",
    "manifest_version",
})


def is_meta_op(name: str) -> bool:
    """True iff `name` is a meta-op (no namespace prefix)."""
    return "." not in name and name in META_OP_NAMES


def dispatch_meta(
    operation: str,
    args: dict[str, Any] | None,
    snapshot: Snapshot,
    live: LiveManifest,
    pool: BackendPool | None,
) -> dict[str, Any]:
    """Single entry point for all meta-op handlers. Each returns a
    JSON-serialisable dict; never raises."""
    if operation == "list":
        return handle_list(snapshot, args)
    if operation == "describe":
        return handle_describe(snapshot, pool, args)
    if operation == "sync":
        return handle_sync(snapshot, live, pool)
    if operation == "health":
        return handle_health(live, pool)
    if operation == "manifest_version":
        return handle_manifest_version(snapshot)
    return {
        "error": f"meta-op {operation!r} declared but not implemented",
        "_internal_bug": "is_meta_op returned True but dispatch_meta has "
                         "no handler — META_OP_NAMES drift?",
    }


def handle_list(snapshot: Snapshot, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return every op in the snapshot, optionally filtered by namespace."""
    args = args or {}
    namespace_filter = args.get("namespace")
    ops_list = list(snapshot.ops)
    if namespace_filter:
        ops_list = [op for op in ops_list if op.namespace == namespace_filter]
    return {
        "snapshot_version": snapshot.snapshot_version,
        "snapshot_hash":    snapshot.hash,
        "ops": [op.to_dict() for op in ops_list],
    }


def handle_describe(
    snapshot: Snapshot,
    pool: BackendPool | None,
    args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return docs + schema for one op.

    For domain ops with a live backend connection, fetches the actual
    JSON schema from the backend's `tools/list` cache. For meta-ops or
    when the pool isn't wired, falls back to snapshot summary only.
    """
    args = args or {}
    name = args.get("operation")
    if not name:
        return {
            "error": "missing required arg 'operation'",
            "expected_args": {"operation": "<name>"},
        }

    match = next((op for op in snapshot.ops if op.name == name), None)
    if not match:
        return {
            "error": f"unknown op: {name!r}",
            "hint": "Call op({operation: \"list\"}) for the full catalog.",
        }

    response: dict[str, Any] = {
        "name":      match.name,
        "namespace": match.namespace,
        "summary":   match.summary,
    }

    # For meta-ops we have no separate backend to query — the snapshot
    # is the source of truth.
    if match.namespace == "meta":
        response["schema_source"] = "meta-op (built into the gateway)"
        response["schema"] = None
        return response

    # Domain op — try the live backend cache for the real schema.
    if pool is None:
        response["schema_source"] = "snapshot-only (no pool wired)"
        response["schema"] = None
        return response

    # Strip the namespace prefix to get the backend's view of the tool name.
    if "." in name:
        _, _, backend_tool_name = name.partition(".")
    else:
        backend_tool_name = name
    tool = pool.find_tool(match.namespace, backend_tool_name)
    if tool is None:
        conn = pool.get(match.namespace)
        status = conn.status.status if conn else "unknown"
        response["schema_source"] = f"backend ({status}) hasn't reported this tool yet"
        response["schema"] = None
        return response

    response["schema_source"]   = "live backend tools/list"
    response["description"]     = getattr(tool, "description", None)
    response["schema"]          = getattr(tool, "inputSchema", None)
    if conn := pool.get(match.namespace):
        response["backend_status"] = conn.status.status
    return response


def handle_sync(
    snapshot: Snapshot,
    live: LiveManifest,
    pool: BackendPool | None,
) -> dict[str, Any]:
    """Diff the snapshot against the live registry. Zero cache cost — the
    SDK's cached tool description doesn't change.

    When a pool is wired, the result includes `changed_schemas`: ops
    whose backend `inputSchema` has drifted since the snapshot was
    promoted. Without a pool, only name-level adds/removes are
    reported (sufficient for the standalone Phase-1 mode)."""
    return diff(snapshot, live, pool).to_dict()


def handle_health(live: LiveManifest, pool: BackendPool | None) -> dict[str, Any]:
    """Per-backend status. Uses live pool data when wired; falls back to
    'not_connected' for everything when the pool isn't initialised."""
    if pool is None:
        return {
            "backends": [
                {
                    "name":   backend.name,
                    "status": "not_connected",
                    "note":   "no backend pool wired (gateway started without --pool)",
                }
                for backend in live.backends
            ],
            "gateway_uptime_secs": None,
        }

    return {
        "backends": [s.to_dict() for s in pool.health()],
    }


def handle_manifest_version(snapshot: Snapshot) -> dict[str, Any]:
    """Snapshot version + hash, for change detection across long-running
    sessions."""
    return {
        "snapshot_version": snapshot.snapshot_version,
        "snapshot_hash":    snapshot.hash,
        "promoted_at":      snapshot.promoted_at,
    }
