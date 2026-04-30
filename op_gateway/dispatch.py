"""Operation dispatch — route an `op({operation, args})` call to the right
handler.

Phase 1 only handles meta-ops. Domain ops (anything with a dot) return a
deterministic 'not yet wired' error so callers can integration-test the
gateway end-to-end without backends.
"""
from __future__ import annotations

from typing import Any

from . import meta_ops
from .manifest import LiveManifest, Snapshot


def dispatch(
    operation: str,
    args: dict[str, Any] | None,
    snapshot: Snapshot,
    live: LiveManifest,
) -> dict[str, Any]:
    """Route a single op call. Returns the JSON-serialisable response that
    `op-gateway`'s MCP layer will hand back to the SDK as the tool result.

    Errors come back as `{"error": "...", ...}` rather than as Python
    exceptions, because the MCP tool-call protocol expects an in-band
    error structure for the agent to read and self-correct."""
    if not isinstance(operation, str) or not operation:
        return {
            "error": "missing or invalid 'operation' parameter",
            "expected": {"operation": "<name>", "args": "<object?>"},
        }

    if meta_ops.is_meta_op(operation):
        return _dispatch_meta(operation, args, snapshot, live)

    if "." not in operation:
        return {
            "error": f"unknown op: {operation!r}",
            "hint": "Meta-ops are unprefixed (list, describe, sync, health, "
                    "manifest_version). Domain ops are dot-prefixed "
                    "(<namespace>.<tool>). Call op({operation: \"list\"}) "
                    "for the full catalog.",
        }

    namespace, _, tool_name = operation.partition(".")
    backend = live.backend_by_name(namespace)
    if backend is None:
        return {
            "error": f"unknown namespace {namespace!r}",
            "hint": "Call op({operation: \"sync\"}) for current state, or "
                    "op({operation: \"list\"}) to see all available namespaces.",
        }

    # Backend exists in live registry. Confirm the tool is declared.
    declared_op = next((o for o in backend.ops if o.name == tool_name), None)
    if declared_op is None:
        return {
            "error": f"backend {namespace!r} has no op named {tool_name!r}",
            "hint": "Call op({operation: \"sync\"}) for current state.",
        }

    # Phase 1: backend connections aren't wired yet. Return a structured
    # placeholder rather than failing silently.
    return {
        "error": "backend dispatch not implemented in Phase 1",
        "phase": 1,
        "operation": operation,
        "would_dispatch_to": {
            "backend": backend.name,
            "command": list(backend.command),
            "tool":    tool_name,
            "args":    args or {},
        },
        "next_phase": "Phase 2 wires backend stdio pools so this returns the "
                      "real backend response.",
    }


def _dispatch_meta(
    operation: str,
    args: dict[str, Any] | None,
    snapshot: Snapshot,
    live: LiveManifest,
) -> dict[str, Any]:
    if operation == "list":
        return meta_ops.handle_list(snapshot, args)
    if operation == "describe":
        return meta_ops.handle_describe(snapshot, args)
    if operation == "sync":
        return meta_ops.handle_sync(snapshot, live)
    if operation == "health":
        return meta_ops.handle_health(live)
    if operation == "manifest_version":
        return meta_ops.handle_manifest_version(snapshot)
    return {
        "error": f"meta-op {operation!r} declared but not implemented",
        "_internal_bug": "meta_ops.is_meta_op returned True but dispatcher "
                         "has no handler — META_OP_NAMES drift?",
    }
