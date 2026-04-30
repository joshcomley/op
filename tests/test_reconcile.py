"""Hot-reload reconciliation: BackendPool.reconcile + the server-side
mtime watcher.

Tests use the real fake MCP backend so connect/disconnect lifecycle
is exercised end-to-end. Adding/removing backends mid-flight should
keep the rest of the pool intact."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from op_gateway.backend_pool import (
    STATUS_UP,
    BackendPool,
    _backend_def_equal,
)
from op_gateway.manifest import BackendDef, OpDef


FAKE_BACKEND = Path(__file__).parent / "fake_backend_mcp.py"


def _backend_def(
    name: str = "fake",
    *,
    extra_arg: str | None = None,
    ops: tuple[OpDef, ...] | None = None,
) -> BackendDef:
    """Build a BackendDef pointing at the fake MCP server. `extra_arg`
    lets tests construct two BackendDefs with the SAME name but
    DIFFERENT command lists, to exercise restart-on-change."""
    cmd: tuple[str, ...] = (sys.executable, str(FAKE_BACKEND))
    if extra_arg:
        cmd = cmd + (extra_arg,)
    return BackendDef(
        name=name,
        command=cmd,
        cwd=None,
        env={},
        ops=ops or (
            OpDef("echo", "Echo a message"),
            OpDef("fail", "Always errors"),
        ),
    )


# --- _backend_def_equal -------------------------------------------------

def test_backend_def_equal_compares_spawn_inputs() -> None:
    """Equality covers command/cwd/env. Differs on any of those."""
    a = _backend_def("a")
    b = _backend_def("a")  # same args
    assert _backend_def_equal(a, b)

    c = _backend_def("a", extra_arg="--something")
    assert not _backend_def_equal(a, c)


def test_backend_def_equal_ignores_ops_list() -> None:
    """Adding an entry to the manifest's `ops` list shouldn't trigger
    a backend restart — the live tools/list is the source of truth.
    `ops` is a manifest hint, not part of the spawn config."""
    a = _backend_def("a", ops=(OpDef("echo", "..."),))
    b = _backend_def("a", ops=(OpDef("echo", "..."), OpDef("fail", "...")))
    assert _backend_def_equal(a, b)


# --- BackendPool.reconcile -----------------------------------------------

async def test_reconcile_starts_new_backend() -> None:
    """A backend appearing in the new manifest but not currently in the
    pool should be spawned and reach `up`."""
    pool = BackendPool([_backend_def("alpha")])
    try:
        await pool.start_all()
        await pool.wait_for_initial_connect(timeout_secs=10.0)

        actions = await pool.reconcile([
            _backend_def("alpha"),
            _backend_def("beta"),
        ])
        assert actions == {"alpha": "unchanged", "beta": "started"}

        # Wait for beta to come up
        beta_conn = pool.get("beta")
        assert beta_conn is not None
        assert await beta_conn.wait_until_up(timeout_secs=10.0)
        assert beta_conn.status.status == STATUS_UP
    finally:
        await pool.stop_all()


async def test_reconcile_stops_removed_backend() -> None:
    """A backend currently in the pool but absent from the new manifest
    should be stopped and removed."""
    pool = BackendPool([_backend_def("alpha"), _backend_def("beta")])
    try:
        await pool.start_all()
        await pool.wait_for_initial_connect(timeout_secs=10.0)
        original_alpha = pool.get("alpha")

        actions = await pool.reconcile([_backend_def("alpha")])
        assert actions == {"alpha": "unchanged", "beta": "stopped"}

        # alpha intact, beta gone
        assert pool.get("beta") is None
        assert pool.get("alpha") is original_alpha
    finally:
        await pool.stop_all()


async def test_reconcile_restarts_changed_backend() -> None:
    """A backend whose command changes should be stopped and respawned
    with the new command. The connection's identity changes (new
    BackendConnection object); existing call_lock and tools cache reset."""
    pool = BackendPool([_backend_def("alpha")])
    try:
        await pool.start_all()
        await pool.wait_for_initial_connect(timeout_secs=10.0)
        original_conn = pool.get("alpha")

        actions = await pool.reconcile([
            _backend_def("alpha", extra_arg="--something-different"),
        ])
        assert actions == {"alpha": "restarted"}

        new_conn = pool.get("alpha")
        assert new_conn is not None
        # Different connection object — fresh state.
        assert new_conn is not original_conn
        # Note: the new command will fail to start (fake_backend_mcp.py
        # doesn't accept --something-different), but reconcile itself
        # has already done the start() call. The supervisor will be
        # in the connecting/down/reconnecting state. We don't assert
        # `up` here because the test backend rejects that argv.
    finally:
        await pool.stop_all()


async def test_reconcile_unchanged_keeps_connection() -> None:
    """If a backend is already in the pool with the same definition,
    reconcile leaves it alone (same connection object, no
    spurious restart)."""
    pool = BackendPool([_backend_def("alpha")])
    try:
        await pool.start_all()
        await pool.wait_for_initial_connect(timeout_secs=10.0)
        original_conn = pool.get("alpha")
        original_started_at = original_conn.status.started_at

        actions = await pool.reconcile([_backend_def("alpha")])
        assert actions == {"alpha": "unchanged"}

        # Same connection object, same started_at, no restart.
        assert pool.get("alpha") is original_conn
        assert original_conn.status.started_at == original_started_at
    finally:
        await pool.stop_all()


async def test_reconcile_combines_actions() -> None:
    """The full mix: one stays, one stops, one restarts, one starts."""
    pool = BackendPool([
        _backend_def("staying"),
        _backend_def("going"),
        _backend_def("changing"),
    ])
    try:
        await pool.start_all()
        await pool.wait_for_initial_connect(timeout_secs=10.0)

        actions = await pool.reconcile([
            _backend_def("staying"),
            _backend_def("changing", extra_arg="--evolved"),
            _backend_def("new"),
        ])
        assert actions["staying"]   == "unchanged"
        assert actions["going"]     == "stopped"
        assert actions["changing"]  == "restarted"
        assert actions["new"]       == "started"
    finally:
        await pool.stop_all()


# --- end-to-end watcher --------------------------------------------------

async def test_watcher_reloads_op_json_on_mtime_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reload watcher should:
      * pick up an op.json mtime change
      * reload the manifest
      * call pool.reconcile
      * update state["live"] to the new manifest

    Tests at the integration level by driving _reload_watcher
    directly with a real BackendPool.
    """
    from op_gateway.server import _reload_watcher
    from op_gateway.manifest import LiveManifest, load_live

    # Initial op.json with one backend
    op_json = tmp_path / "op.json"
    op_json.write_text(json.dumps({
        "registry_version": "1",
        "env": {},
        "backends": [{
            "name": "alpha",
            "command": [sys.executable, str(FAKE_BACKEND)],
            "ops": [{"name": "echo", "summary": "..."}],
        }],
    }), encoding="utf-8")

    initial_live = load_live(op_json)
    pool = BackendPool(list(initial_live.backends))
    state: dict = {"pool": pool, "live": initial_live, "snapshot": None}

    try:
        await pool.start_all()
        await pool.wait_for_initial_connect(timeout_secs=10.0)
        assert set(pool.names()) == {"alpha"}

        # Drive the watcher manually with a tight poll cadence.
        # We can't easily monkeypatch _RELOAD_POLL_INTERVAL_SECS at
        # call time, so just kick the watcher and rewrite the file
        # to trigger a reload.
        from op_gateway import server as _srv
        monkeypatch.setattr(_srv, "_RELOAD_POLL_INTERVAL_SECS", 0.05)

        watcher = asyncio.create_task(_reload_watcher(state, op_json))
        try:
            await asyncio.sleep(0.1)  # let watcher take its first stat

            # Rewrite op.json with two backends. The watcher should pick
            # this up and call reconcile.
            op_json.write_text(json.dumps({
                "registry_version": "1",
                "env": {},
                "backends": [
                    {
                        "name": "alpha",
                        "command": [sys.executable, str(FAKE_BACKEND)],
                        "ops": [{"name": "echo", "summary": "..."}],
                    },
                    {
                        "name": "beta",
                        "command": [sys.executable, str(FAKE_BACKEND)],
                        "ops": [{"name": "echo", "summary": "..."}],
                    },
                ],
            }), encoding="utf-8")
            # Bump mtime explicitly — some filesystems have coarse
            # mtime resolution and the rewrite might be too fast to
            # register a difference otherwise.
            import os, time
            future = time.time() + 5.0
            os.utime(op_json, (future, future))

            # Wait for the watcher to notice + reconcile + update state.
            for _ in range(80):  # ~8s upper bound
                if "beta" in pool.names() and state["live"] is not initial_live:
                    break
                await asyncio.sleep(0.1)
            assert set(pool.names()) == {"alpha", "beta"}
            assert state["live"] is not initial_live
            # The new live must declare both backends.
            assert {b.name for b in state["live"].backends} == {"alpha", "beta"}

            beta_conn = pool.get("beta")
            assert beta_conn is not None
            assert await beta_conn.wait_until_up(timeout_secs=10.0)
        finally:
            watcher.cancel()
            try:
                await watcher
            except (asyncio.CancelledError, Exception):
                pass
    finally:
        await pool.stop_all()


async def test_watcher_keeps_state_on_parse_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If op.json becomes malformed mid-edit (trailing comma etc.),
    the watcher must NOT tear down the pool. Pool stays as-is, the
    error is logged, polling continues until the file is valid again."""
    from op_gateway.server import _reload_watcher
    from op_gateway.manifest import load_live

    op_json = tmp_path / "op.json"
    op_json.write_text(json.dumps({
        "registry_version": "1",
        "env": {},
        "backends": [{
            "name": "alpha",
            "command": [sys.executable, str(FAKE_BACKEND)],
            "ops": [{"name": "echo", "summary": "..."}],
        }],
    }), encoding="utf-8")

    initial_live = load_live(op_json)
    pool = BackendPool(list(initial_live.backends))
    state: dict = {"pool": pool, "live": initial_live, "snapshot": None}

    try:
        await pool.start_all()
        await pool.wait_for_initial_connect(timeout_secs=10.0)

        from op_gateway import server as _srv
        monkeypatch.setattr(_srv, "_RELOAD_POLL_INTERVAL_SECS", 0.05)
        watcher = asyncio.create_task(_reload_watcher(state, op_json))
        try:
            # Write invalid JSON
            op_json.write_text("{ this is not valid json", encoding="utf-8")
            import os, time
            future = time.time() + 5.0
            os.utime(op_json, (future, future))

            # Give the watcher time to notice + log the error.
            await asyncio.sleep(0.5)

            # Pool should be intact — alpha still up.
            assert set(pool.names()) == {"alpha"}
            assert state["live"] is initial_live  # unchanged
            alpha_conn = pool.get("alpha")
            assert alpha_conn is not None
            assert alpha_conn.status.status == STATUS_UP
        finally:
            watcher.cancel()
            try:
                await watcher
            except (asyncio.CancelledError, Exception):
                pass
    finally:
        await pool.stop_all()
