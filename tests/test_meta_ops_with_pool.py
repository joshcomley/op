"""Meta-ops with a live backend pool — `describe` returns real schemas,
`health` reports real statuses.

The pool-less meta-op behaviour is covered by `test_dispatch.py`. These
tests pin the live-pool side of the same handlers."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from op_gateway import dispatch, meta_ops
from op_gateway.backend_pool import STATUS_UP, BackendPool
from op_gateway.manifest import (
    BackendDef,
    LiveManifest,
    OpDef,
    Snapshot,
    SnapshotEntry,
)


FAKE_BACKEND = Path(__file__).parent / "fake_backend_mcp.py"


def _backend() -> BackendDef:
    return BackendDef(
        name="fake",
        command=(sys.executable, str(FAKE_BACKEND)),
        cwd=None,
        env={},
        ops=(
            OpDef(name="echo", summary="Echo a message"),
            OpDef(name="fail", summary="Always errors"),
        ),
    )


def _snap() -> Snapshot:
    return Snapshot(
        snapshot_version="0.0.1",
        promoted_at="2026-04-30T00:00:00Z",
        hash="sha256:placeholder",
        highlights=(),
        ops=(
            SnapshotEntry("meta",  "list",            "Enumerate available ops"),
            SnapshotEntry("meta",  "describe",        "Schema + docs for one op"),
            SnapshotEntry("meta",  "sync",            "Diff vs current live registry"),
            SnapshotEntry("meta",  "health",          "Per-backend availability"),
            SnapshotEntry("meta",  "manifest_version","Snapshot version + hash"),
            SnapshotEntry("fake",  "fake.echo",       "Echo a message"),
            SnapshotEntry("fake",  "fake.fail",       "Always errors"),
        ),
    )


def _live() -> LiveManifest:
    return LiveManifest(
        registry_version="1",
        interpolation_env={},
        backends=(_backend(),),
    )


async def test_describe_returns_live_schema_for_domain_op() -> None:
    """`describe` should fetch the real JSON schema from the backend's
    cached `tools/list` once the pool is up. This is what lets the agent
    learn argument shapes without snapshot-side maintenance."""
    pool = BackendPool([_backend()])
    try:
        await pool.start_all()
        await pool.wait_for_initial_connect(timeout_secs=10.0)
        result = meta_ops.handle_describe(
            _snap(), pool, {"operation": "fake.echo"},
        )
        assert result["name"] == "fake.echo"
        assert result["schema_source"] == "live backend tools/list"
        schema = result["schema"]
        assert isinstance(schema, dict)
        assert "properties" in schema
        assert "message" in schema["properties"]
        assert result.get("backend_status") == STATUS_UP
    finally:
        await pool.stop_all()


async def test_describe_meta_op_uses_built_in_marker() -> None:
    """`describe` for a meta-op shouldn't try to query a backend.

    Returns schema=None with schema_source noting it's a meta-op."""
    pool = BackendPool([])
    try:
        await pool.start_all()
        result = meta_ops.handle_describe(
            _snap(), pool, {"operation": "list"},
        )
        assert result["name"] == "list"
        assert result["schema"] is None
        assert "meta-op" in result["schema_source"].lower()
    finally:
        await pool.stop_all()


async def test_health_with_pool_returns_real_statuses() -> None:
    """`health` reports the pool's live BackendStatus dicts when wired."""
    pool = BackendPool([_backend()])
    try:
        await pool.start_all()
        await pool.wait_for_initial_connect(timeout_secs=10.0)
        result = meta_ops.handle_health(_live(), pool)
        assert "backends" in result
        backends = result["backends"]
        assert len(backends) == 1
        assert backends[0]["name"] == "fake"
        assert backends[0]["status"] == STATUS_UP
        assert backends[0]["uptime_secs"] >= 0
        assert backends[0].get("tool_count") == 2
    finally:
        await pool.stop_all()


async def test_full_dispatch_with_pool_routes_to_backend() -> None:
    """End-to-end through the public dispatcher: a domain op call goes
    out to the fake backend and the response comes back as a JSON-
    serialisable content-block dict."""
    pool = BackendPool([_backend()])
    try:
        await pool.start_all()
        await pool.wait_for_initial_connect(timeout_secs=10.0)
        result = await dispatch.dispatch(
            "fake.echo", {"message": "hi"}, _snap(), _live(), pool,
        )
        # Phase-1 placeholder shape would have phase=1; real dispatch
        # returns the backend's CallToolResult, which has `content`.
        assert result.get("phase") != 1
        assert "content" in result
        text = "".join(
            b.get("text", "")
            for b in result["content"]
            if isinstance(b, dict) and b.get("type") == "text"
        )
        assert "echo: hi" in text
    finally:
        await pool.stop_all()


async def test_full_dispatch_unknown_namespace_with_pool() -> None:
    """Unknown namespace stays unknown even with a pool wired — the
    live manifest is the gate."""
    pool = BackendPool([_backend()])
    try:
        await pool.start_all()
        await pool.wait_for_initial_connect(timeout_secs=10.0)
        result = await dispatch.dispatch(
            "ghost.nope", {}, _snap(), _live(), pool,
        )
        assert "error" in result
        assert "namespace" in result["error"].lower()
    finally:
        await pool.stop_all()


async def test_full_dispatch_unknown_tool_with_pool() -> None:
    """A tool name not in the manifest AND not in the live catalog is
    rejected before reaching the backend (saves a round-trip)."""
    pool = BackendPool([_backend()])
    try:
        await pool.start_all()
        await pool.wait_for_initial_connect(timeout_secs=10.0)
        result = await dispatch.dispatch(
            "fake.imaginary", {}, _snap(), _live(), pool,
        )
        assert "error" in result
        assert "imaginary" in result["error"]
    finally:
        await pool.stop_all()
