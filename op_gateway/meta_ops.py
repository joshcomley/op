"""Meta-op handlers — list, describe, sync, health, manifest_version.

Phase 1: backends aren't connected yet, so `describe` and `health` return
the snapshot's static catalog rather than live backend data. Phase 2
extends them to call into a live backend pool.
"""
from __future__ import annotations

from typing import Any

from .diff import diff
from .manifest import LiveManifest, Snapshot, SnapshotEntry


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


def handle_describe(snapshot: Snapshot, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return docs for one op. Phase 1 returns only the snapshot summary;
    Phase 2 will fetch live schema + extended description from the backend."""
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
    return {
        "name":          match.name,
        "namespace":     match.namespace,
        "summary":       match.summary,
        "schema":        None,    # Phase 2: fetch from live backend
        "schema_source": "snapshot-only (Phase 1; no live backend connection yet)",
    }


def handle_sync(snapshot: Snapshot, live: LiveManifest) -> dict[str, Any]:
    """Diff the snapshot against the live registry. Zero cache cost — the
    SDK's cached tool description doesn't change."""
    return diff(snapshot, live).to_dict()


def handle_health(live: LiveManifest) -> dict[str, Any]:
    """Per-backend status. Phase 1: report 'not_connected' for every backend
    since we haven't spawned any yet. Phase 2 reports real states."""
    return {
        "backends": [
            {
                "name":   backend.name,
                "status": "not_connected",
                "note":   "Phase 1 placeholder; backend pool wiring lands in Phase 2.",
            }
            for backend in live.backends
        ],
        "gateway_uptime_secs": None,    # Phase 2: track since gateway start
    }


def handle_manifest_version(snapshot: Snapshot) -> dict[str, Any]:
    """Snapshot version + hash, for change detection across long-running
    sessions."""
    return {
        "snapshot_version": snapshot.snapshot_version,
        "snapshot_hash":    snapshot.hash,
        "promoted_at":      snapshot.promoted_at,
    }
