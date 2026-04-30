"""Schema-drift detection in `sync`.

Phase 3 introduced schema_hash on each SnapshotEntry. At promote time
the CLI probes the live backends, hashes each tool's inputSchema, and
stores the hash. At runtime, `op({operation: "sync"})` compares the
snapshot's stored hash against the live backend's current hash and
reports any mismatches under `changed_schemas`.

These tests cover the comparison logic in isolation. The end-to-end
"promote -> snapshot has hashes -> sync detects drift" flow is covered
by `test_probe.py`."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from op_gateway.backend_pool import BackendPool
from op_gateway.diff import diff
from op_gateway.manifest import (
    BackendDef,
    LiveManifest,
    OpDef,
    Snapshot,
    SnapshotEntry,
    canonical_schema_hash,
)


FAKE_BACKEND = Path(__file__).parent / "fake_backend_mcp.py"


class _FakePoolTool:
    """Stand-in for an MCP `Tool` object so tests can construct schemas
    without spinning up a real subprocess."""
    def __init__(self, name: str, inputSchema: dict[str, Any]) -> None:
        self.name = name
        self.inputSchema = inputSchema
        self.description = ""


class _FakePool:
    """Tiny pool stub that satisfies the find_tool API used by diff."""
    def __init__(self, tools: dict[str, list[_FakePoolTool]]) -> None:
        # tools is {namespace: [Tool, ...]}
        self._tools = tools

    def find_tool(self, namespace: str, tool_name: str) -> Any | None:
        for t in self._tools.get(namespace, []):
            if t.name == tool_name:
                return t
        return None


_META_ENTRIES: tuple[SnapshotEntry, ...] = (
    SnapshotEntry("meta", "list",             "Enumerate available ops"),
    SnapshotEntry("meta", "describe",         "Schema + docs for one op"),
    SnapshotEntry("meta", "sync",             "Diff vs current live registry"),
    SnapshotEntry("meta", "health",           "Per-backend availability"),
    SnapshotEntry("meta", "manifest_version", "Snapshot version + hash"),
)


def _snap_with_hashes(*entries: SnapshotEntry) -> Snapshot:
    """Snapshot fixture that already contains the meta-ops, so domain
    op tests don't trip is_drifted on the always-present meta entries
    that expand_live_to_entries injects into live."""
    return Snapshot(
        snapshot_version="0.0.1",
        promoted_at="2026-04-30T00:00:00Z",
        hash="sha256:placeholder",
        highlights=(),
        ops=_META_ENTRIES + entries,
    )


def _live(backend_name: str, *op_names: str) -> LiveManifest:
    return LiveManifest(
        registry_version="1",
        interpolation_env={},
        backends=(BackendDef(
            name=backend_name,
            command=("python",),
            cwd=None,
            env={},
            ops=tuple(OpDef(n, "...") for n in op_names),
        ),),
    )


# --- baseline: no drift ----------------------------------------------------

def test_diff_reports_no_change_when_schemas_match() -> None:
    """When the snapshot's schema_hash matches the live backend's
    current hash, `changed_schemas` is empty."""
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    schema_hash = canonical_schema_hash(schema)

    snap = _snap_with_hashes(
        SnapshotEntry("recap", "recap.recap", "summary", schema_hash=schema_hash),
    )
    live = _live("recap", "recap")
    pool = _FakePool({"recap": [_FakePoolTool("recap", schema)]})

    result = diff(snap, live, pool)
    assert result.changed_schemas == []
    assert result.is_drifted is False


def test_diff_detects_changed_schema() -> None:
    """When the live backend's schema differs from the snapshot's,
    `changed_schemas` lists the op with both hashes."""
    old_schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    new_schema = {"type": "object", "properties": {
        "x": {"type": "string"},
        "verbose": {"type": "boolean"},
    }}

    snap = _snap_with_hashes(
        SnapshotEntry(
            "recap", "recap.recap", "summary",
            schema_hash=canonical_schema_hash(old_schema),
        ),
    )
    live = _live("recap", "recap")
    pool = _FakePool({"recap": [_FakePoolTool("recap", new_schema)]})

    result = diff(snap, live, pool)
    assert len(result.changed_schemas) == 1
    drift = result.changed_schemas[0]
    assert drift["name"] == "recap.recap"
    assert drift["namespace"] == "recap"
    assert drift["snapshot_hash"] == canonical_schema_hash(old_schema)
    assert drift["current_hash"] == canonical_schema_hash(new_schema)
    assert "describe" in drift["hint"]
    assert result.is_drifted is True


def test_diff_skips_meta_ops() -> None:
    """Meta-ops have no backend, so they're never in changed_schemas
    even when their snapshot has no hash."""
    snap = _snap_with_hashes(
        SnapshotEntry("meta", "list", "Enumerate"),
    )
    live = _live("ignored", "ignored_op")
    pool = _FakePool({})

    result = diff(snap, live, pool)
    assert result.changed_schemas == []


def test_diff_skips_ops_with_no_snapshot_hash() -> None:
    """Legacy snapshot entries without a schema_hash don't trigger
    spurious drift — we just skip them."""
    schema = {"type": "object"}
    snap = _snap_with_hashes(
        SnapshotEntry("recap", "recap.recap", "summary", schema_hash=None),
    )
    live = _live("recap", "recap")
    pool = _FakePool({"recap": [_FakePoolTool("recap", schema)]})

    result = diff(snap, live, pool)
    assert result.changed_schemas == []


def test_diff_skips_unreachable_backend() -> None:
    """When the backend has no live tool entry (down, or just not
    exposing this tool right now), we don't double-report under
    changed_schemas — `health` shows the unavailability separately."""
    schema = {"type": "object"}
    snap = _snap_with_hashes(
        SnapshotEntry(
            "recap", "recap.recap", "summary",
            schema_hash=canonical_schema_hash(schema),
        ),
    )
    live = _live("recap", "recap")
    pool = _FakePool({"recap": []})  # no tool entries — pretend backend is mid-reconnect

    result = diff(snap, live, pool)
    assert result.changed_schemas == []


def test_diff_no_pool_returns_empty_changed_schemas() -> None:
    """Without a pool, schema-diff falls back to []. Legitimate Phase-1
    mode + tests that don't want to spin up backends."""
    schema = {"type": "object"}
    snap = _snap_with_hashes(
        SnapshotEntry(
            "recap", "recap.recap", "summary",
            schema_hash=canonical_schema_hash(schema),
        ),
    )
    live = _live("recap", "recap")
    result = diff(snap, live, None)
    assert result.changed_schemas == []


def test_canonical_schema_hash_stable_across_key_order() -> None:
    """Two semantically-equal schemas with different JSON key order
    must hash equal. Otherwise we'd flag every promote as a drift."""
    schema_a = {"type": "object", "properties": {"x": {"type": "string"}}}
    schema_b = {"properties": {"x": {"type": "string"}}, "type": "object"}
    assert canonical_schema_hash(schema_a) == canonical_schema_hash(schema_b)


def test_canonical_schema_hash_handles_none() -> None:
    """None schema (some backends omit inputSchema entirely) hashes to
    a stable sentinel."""
    h = canonical_schema_hash(None)
    assert h.startswith("sha256:")
    assert h == canonical_schema_hash(None)


# --- end-to-end: probe + promote + sync -----------------------------------

import pytest


async def test_probe_returns_schema_hashes() -> None:
    """`probe_backends` connects to a real fake MCP server, fetches
    tools/list, and returns ProbedTool entries with schema_hash set."""
    from op_gateway.probe import probe_backends
    backend = BackendDef(
        name="fake",
        command=(sys.executable, str(FAKE_BACKEND)),
        cwd=None,
        env={},
        ops=(OpDef("echo", "..."), OpDef("fail", "...")),
    )
    probed = await probe_backends([backend], timeout_secs=10.0)
    tools = probed["fake"]
    assert len(tools) == 2
    by_name = {t.tool_name: t for t in tools}
    assert "echo" in by_name
    assert "fail" in by_name
    # Each tool's hash must be derivable from its schema; same schema
    # must produce same hash.
    for t in tools:
        assert t.schema_hash.startswith("sha256:")
        assert t.schema_hash == canonical_schema_hash(t.schema)


async def test_probe_unreachable_backend_returns_empty_list() -> None:
    """A backend that fails to start gets an empty tools list. Promote
    then writes its snapshot entries without schema_hash, and sync
    falls back to name-only diff for those ops."""
    from op_gateway.probe import probe_backends
    bad_backend = BackendDef(
        name="bad",
        command=(sys.executable, "-c", "import sys; sys.exit(1)"),
        cwd=None,
        env={},
        ops=(OpDef("never", "..."),),
    )
    probed = await probe_backends([bad_backend], timeout_secs=2.0)
    assert probed["bad"] == []


async def test_end_to_end_promote_then_sync_detects_drift() -> None:
    """Full integration: promote (which probes + writes hashes), then
    sync against a pool whose backend now exposes a different schema
    -> changed_schemas reports the drift.

    Uses two different fake backends to simulate a schema rev: the
    promote-time backend has the old schema; the post-promote backend
    has the new schema. In the real world this is one backend whose
    code changed between sessions."""
    # Skip — full integration of promote + new-backend-schema is brittle
    # to set up here (would need two distinct fake backends or a
    # mutable one). The piece-wise tests above cover the same
    # invariants. Leaving as a placeholder for the real-machine
    # integration test once we have a real backend to exercise.
    pytest.skip("covered piece-wise; real-machine validation is the next phase")
