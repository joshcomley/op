"""Backend pool — supervisor lifecycle, dispatch, error handling.

These tests spawn a real MCP stdio child (`tests/fake_backend_mcp.py`)
so the SDK protocol and our pool's lifecycle interact end-to-end.
Slower than mock-based tests (~1-2s each) but they cover the actual
JSON-RPC framing + handshake — exactly the parts mocks would skip.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from op_gateway.backend_pool import (
    STATUS_DOWN,
    STATUS_UP,
    BackendPool,
    BackendUnavailable,
)
from op_gateway.manifest import BackendDef, OpDef


FAKE_BACKEND = Path(__file__).parent / "fake_backend_mcp.py"


def _backend_def(name: str = "fake", *, command: tuple[str, ...] | None = None,
                 ops: tuple[OpDef, ...] | None = None) -> BackendDef:
    """Build a BackendDef pointing at the fake MCP server."""
    return BackendDef(
        name=name,
        command=command or (sys.executable, str(FAKE_BACKEND)),
        cwd=None,
        env={},
        ops=ops or (
            OpDef(name="echo", summary="Echo a message"),
            OpDef(name="fail", summary="Always errors"),
        ),
    )


async def test_pool_starts_and_reaches_up() -> None:
    """A backend with a valid command reaches `up` within a few seconds
    and exposes its tools/list catalog."""
    pool = BackendPool([_backend_def()])
    try:
        await pool.start_all()
        results = await pool.wait_for_initial_connect(timeout_secs=10.0)
        assert results == {"fake": True}
        conn = pool.get("fake")
        assert conn is not None
        assert conn.status.status == STATUS_UP
        # The fake server exposes echo + fail. The pool should have
        # both in its cached catalog.
        names = {getattr(t, "name", None) for t in conn.tools}
        assert {"echo", "fail"} <= names
    finally:
        await pool.stop_all()


async def test_pool_call_tool_routes_to_backend() -> None:
    """A successful tools/call returns the backend's response in
    FastMCP-friendly content-block shape."""
    pool = BackendPool([_backend_def()])
    try:
        await pool.start_all()
        await pool.wait_for_initial_connect(timeout_secs=10.0)
        result = await pool.call_tool("fake", "echo", {"message": "ping"})
        # Fake backend returns "echo: ping" as a text block. The MCP SDK
        # wraps that in a CallToolResult with content[].
        assert "content" in result
        text_blocks = [
            b for b in result["content"]
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        assert any("echo: ping" in b.get("text", "") for b in text_blocks)
    finally:
        await pool.stop_all()


async def test_pool_call_tool_propagates_backend_error() -> None:
    """When the backend's tool raises, the pool returns isError=True
    in the call result. The agent sees a tool_use_error rather than
    an exception."""
    pool = BackendPool([_backend_def()])
    try:
        await pool.start_all()
        await pool.wait_for_initial_connect(timeout_secs=10.0)
        result = await pool.call_tool("fake", "fail", {"reason": "test"})
        assert result.get("isError") is True
    finally:
        await pool.stop_all()


async def test_pool_unknown_namespace_raises() -> None:
    """Calling a backend that wasn't declared raises BackendUnavailable."""
    pool = BackendPool([_backend_def()])
    try:
        await pool.start_all()
        await pool.wait_for_initial_connect(timeout_secs=10.0)
        with pytest.raises(BackendUnavailable):
            await pool.call_tool("not_declared", "echo", {})
    finally:
        await pool.stop_all()


async def test_pool_invalid_command_marks_backend_down() -> None:
    """A backend whose command can't be spawned doesn't reach `up`.

    `wait_for_initial_connect` returns False for it, and the status
    reflects `down`/`reconnecting` with a last_error attached. This
    is what the `health` meta-op surfaces to the agent."""
    bad = BackendDef(
        name="bad",
        command=(sys.executable, "-c", "import sys; sys.exit(1)"),
        cwd=None,
        env={},
        ops=(OpDef(name="never", summary="..."),),
    )
    pool = BackendPool([bad])
    try:
        await pool.start_all()
        results = await pool.wait_for_initial_connect(timeout_secs=2.0)
        assert results == {"bad": False}
        conn = pool.get("bad")
        assert conn is not None
        # The supervisor will be retrying; either DOWN (first attempt
        # failed, awaiting first retry) or RECONNECTING (already retried).
        assert conn.status.status != STATUS_UP
        assert conn.status.last_error is not None
    finally:
        await pool.stop_all()


async def test_pool_health_reports_per_backend_status() -> None:
    """`pool.health()` returns one BackendStatus per backend with
    the right names."""
    pool = BackendPool([
        _backend_def("alpha"),
        _backend_def("beta"),
    ])
    try:
        await pool.start_all()
        await pool.wait_for_initial_connect(timeout_secs=10.0)
        statuses = pool.health()
        names = [s.name for s in statuses]
        assert names == ["alpha", "beta"]
        for s in statuses:
            assert s.status == STATUS_UP
            d = s.to_dict()
            assert d["name"] in {"alpha", "beta"}
            assert d["status"] == STATUS_UP
            assert d["uptime_secs"] >= 0
    finally:
        await pool.stop_all()


async def test_pool_find_tool_returns_live_schema() -> None:
    """`find_tool` returns the live MCP Tool object (with schema) so
    `describe` can surface real argument shapes to the agent."""
    pool = BackendPool([_backend_def()])
    try:
        await pool.start_all()
        await pool.wait_for_initial_connect(timeout_secs=10.0)
        tool = pool.find_tool("fake", "echo")
        assert tool is not None
        # MCP Tool has .name, .description, .inputSchema attributes.
        assert getattr(tool, "name", None) == "echo"
        schema = getattr(tool, "inputSchema", None)
        assert schema is not None
        assert "properties" in schema
        assert "message" in schema["properties"]
        assert pool.find_tool("fake", "doesnt_exist") is None
        assert pool.find_tool("no_such_backend", "echo") is None
    finally:
        await pool.stop_all()


async def test_pool_stop_terminates_supervisors() -> None:
    """After stop_all, every backend's supervisor task is done and the
    transports are closed. Re-starting the pool would spawn fresh."""
    pool = BackendPool([_backend_def()])
    await pool.start_all()
    await pool.wait_for_initial_connect(timeout_secs=10.0)
    await pool.stop_all()
    conn = pool.get("fake")
    assert conn is not None
    # _supervisor cleared after stop
    assert conn._supervisor is None or conn._supervisor.done()
