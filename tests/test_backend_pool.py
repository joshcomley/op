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
    EXTRA_INHERITED_ENV_VARS,
    STATUS_DOWN,
    STATUS_UP,
    BackendPool,
    BackendUnavailable,
    _compose_env,
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


# ─────────────────────────────────────────────────────────────────────
# _compose_env — env composition for spawned backends
# ─────────────────────────────────────────────────────────────────────
#
# Why these tests matter:
#   The MCP Python SDK only forwards a tiny safelist (PATH, USERPROFILE,
#   etc.) to spawned children. Any backend that needs to run `git fetch`
#   would otherwise lose `GIT_ASKPASS` and fall back to /dev/tty in a
#   TTY-less subprocess — credential prompts hang or fail. `_compose_env`
#   restores those critical-but-unsafelisted vars from the gateway's own
#   env without breaking the SDK's sandboxing intent for everything else.

def test_compose_env_includes_sdk_safelist() -> None:
    """The SDK's default safelist (PATH, USERPROFILE, etc.) must remain
    present so backends can find executables and resolve $HOME."""
    composed = _compose_env({})
    # PATH is in EVERY OS's safelist; assert the easy-to-verify one.
    import os as _os
    if _os.environ.get("PATH"):
        assert "PATH" in composed
        assert composed["PATH"] == _os.environ["PATH"]


def test_compose_env_forwards_git_askpass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without this, mcpup-mcp's `git fetch` falls back to /dev/tty and
    fails inside a TTY-less subprocess. This is the regression that
    motivated the helper."""
    monkeypatch.setenv("GIT_ASKPASS", r"C:\ProgramData\bot-auth\askpass.cmd")
    composed = _compose_env({})
    assert composed.get("GIT_ASKPASS") == r"C:\ProgramData\bot-auth\askpass.cmd"


def test_compose_env_forwards_github_pats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backends running `gh` or hitting the GitHub API directly need
    GH_TOKEN / GH_WRITE_TOKEN to authenticate."""
    monkeypatch.setenv("GH_TOKEN", "ghp_read")
    monkeypatch.setenv("GH_WRITE_TOKEN", "ghp_write")
    composed = _compose_env({})
    assert composed.get("GH_TOKEN") == "ghp_read"
    assert composed.get("GH_WRITE_TOKEN") == "ghp_write"


def test_compose_env_skips_unset_extras(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Extras that aren't in the gateway's env shouldn't appear in the
    composed dict — we don't synthesise empty strings."""
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    composed = _compose_env({})
    assert "GIT_SSH_COMMAND" not in composed


def test_compose_env_defn_env_wins_over_inherited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backend's per-server env override beats the inherited value.
    Same precedence the SDK gives when defn.env is non-empty — preserved
    here so existing op.json overrides keep their meaning."""
    monkeypatch.setenv("GIT_ASKPASS", "from-gateway")
    composed = _compose_env({"GIT_ASKPASS": "from-defn"})
    assert composed["GIT_ASKPASS"] == "from-defn"


def test_compose_env_works_with_empty_defn_env() -> None:
    """An empty defn.env (the common case) must still produce a usable
    env: SDK safelist + extras intact, no exceptions."""
    composed = _compose_env({})
    # PATH should be inherited from the SDK safelist
    assert isinstance(composed, dict)
    # And the result is non-empty even when defn.env is empty
    assert len(composed) > 0


def test_compose_env_extra_list_includes_critical_auth_vars() -> None:
    """Pin the EXTRA_INHERITED_ENV_VARS contents — these are the vars
    backends practically can't function without on this machine.
    Adding more is fine; removing any of these is a regression."""
    must_have = {
        "GIT_ASKPASS",
        "SSH_AUTH_SOCK",
        "GH_TOKEN",
        "GH_WRITE_TOKEN",
        "PYTHONUTF8",
    }
    assert must_have.issubset(EXTRA_INHERITED_ENV_VARS)


def test_compose_env_skips_function_export_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bash function exports (values starting with `()`) are a known
    security risk and the SDK explicitly skips them. Mirror that here
    so a malicious GIT_ASKPASS-shaped function export doesn't get
    forwarded."""
    monkeypatch.setenv("GIT_ASKPASS", "() { malicious; }")
    composed = _compose_env({})
    assert "GIT_ASKPASS" not in composed
