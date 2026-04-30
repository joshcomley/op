"""Operation dispatch — meta-ops + domain-op routing.

These tests run with `pool=None` to exercise the dispatcher's logic in
isolation. The pool's behaviour (real backend connections) is covered
by `test_backend_pool.py`."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from op_gateway import dispatch
from op_gateway.manifest import load_live, load_snapshot


def _setup(tmp_path: Path, *, backends: list[dict] | None = None,
           snap_ops: list[dict] | None = None) -> tuple:
    live_p = tmp_path / "op.json"
    snap_p = tmp_path / "op.snapshot.json"
    live_p.write_text(json.dumps({
        "registry_version": "1",
        "env": {},
        "backends": backends or [],
    }), encoding="utf-8")
    snap_p.write_text(json.dumps({
        "snapshot_version": "0.0.1",
        "promoted_at": "2026-04-30T00:00:00Z",
        "hash": "sha256:placeholder",
        "highlights": [],
        "ops": snap_ops or [
            {"namespace": "meta", "name": "list",             "summary": "Enumerate available ops"},
            {"namespace": "meta", "name": "describe",         "summary": "Schema + docs for one op"},
            {"namespace": "meta", "name": "sync",             "summary": "Diff vs current live registry"},
            {"namespace": "meta", "name": "health",           "summary": "Per-backend availability"},
            {"namespace": "meta", "name": "manifest_version", "summary": "Snapshot version + hash"},
        ],
    }), encoding="utf-8")
    return load_snapshot(snap_p), load_live(live_p)


async def test_dispatch_list_returns_catalog(tmp_path: Path) -> None:
    snap, live = _setup(tmp_path)
    result = await dispatch.dispatch("list", None, snap, live, None)
    assert "ops" in result
    names = {op["name"] for op in result["ops"]}
    assert {"list", "describe", "sync", "health", "manifest_version"} <= names


async def test_dispatch_list_filters_by_namespace(tmp_path: Path) -> None:
    snap, live = _setup(tmp_path, snap_ops=[
        {"namespace": "meta",     "name": "list",        "summary": "..."},
        {"namespace": "recap",    "name": "recap.recap", "summary": "..."},
        {"namespace": "chatfork", "name": "chatfork.fork", "summary": "..."},
    ])
    result = await dispatch.dispatch("list", {"namespace": "recap"}, snap, live, None)
    names = {op["name"] for op in result["ops"]}
    assert names == {"recap.recap"}


async def test_dispatch_describe_known_op(tmp_path: Path) -> None:
    snap, live = _setup(tmp_path, snap_ops=[
        {"namespace": "recap", "name": "recap.recap", "summary": "Catch up on work"},
    ])
    result = await dispatch.dispatch(
        "describe", {"operation": "recap.recap"}, snap, live, None,
    )
    assert result["name"] == "recap.recap"
    assert result["summary"] == "Catch up on work"
    assert "error" not in result


async def test_dispatch_describe_missing_arg(tmp_path: Path) -> None:
    snap, live = _setup(tmp_path)
    result = await dispatch.dispatch("describe", {}, snap, live, None)
    assert "error" in result
    assert "operation" in result["error"]


async def test_dispatch_describe_unknown_op(tmp_path: Path) -> None:
    snap, live = _setup(tmp_path)
    result = await dispatch.dispatch(
        "describe", {"operation": "no.such.op"}, snap, live, None,
    )
    assert "error" in result
    assert "unknown" in result["error"].lower()


async def test_dispatch_sync_returns_diff(tmp_path: Path) -> None:
    snap, live = _setup(tmp_path, backends=[
        {
            "name": "recap",
            "command": ["node", "/x"],
            "ops": [{"name": "recap_new", "summary": "newly added"}],
        },
    ])
    result = await dispatch.dispatch("sync", None, snap, live, None)
    assert result["is_drifted"] is True
    assert any(op["name"] == "recap.recap_new" for op in result["added"])


async def test_dispatch_health_lists_all_backends(tmp_path: Path) -> None:
    snap, live = _setup(tmp_path, backends=[
        {"name": "recap",     "command": ["node", "/r"], "ops": [{"name": "recap", "summary": "..."}]},
        {"name": "chatfork",  "command": ["node", "/c"], "ops": [{"name": "fork",  "summary": "..."}]},
    ])
    result = await dispatch.dispatch("health", None, snap, live, None)
    names = {b["name"] for b in result["backends"]}
    assert names == {"recap", "chatfork"}
    # No pool wired -> every backend reports not_connected.
    assert all(b["status"] == "not_connected" for b in result["backends"])


async def test_dispatch_manifest_version_returns_hash(tmp_path: Path) -> None:
    snap, live = _setup(tmp_path)
    result = await dispatch.dispatch("manifest_version", None, snap, live, None)
    assert "snapshot_version" in result
    assert "snapshot_hash" in result
    assert result["snapshot_hash"].startswith("sha256:")


async def test_dispatch_unknown_meta_op_falls_through_with_error(tmp_path: Path) -> None:
    """A name with no dot but not in META_OP_NAMES is genuinely unknown."""
    snap, live = _setup(tmp_path)
    result = await dispatch.dispatch("frobnicate", None, snap, live, None)
    assert "error" in result
    assert "unknown op" in result["error"].lower()


async def test_dispatch_domain_op_unknown_namespace(tmp_path: Path) -> None:
    snap, live = _setup(tmp_path)
    result = await dispatch.dispatch(
        "missing_backend.foo", None, snap, live, None,
    )
    assert "error" in result
    assert "namespace" in result["error"].lower()


async def test_dispatch_domain_op_known_namespace_unknown_tool(tmp_path: Path) -> None:
    snap, live = _setup(tmp_path, backends=[
        {
            "name": "recap",
            "command": ["node", "/x"],
            "ops": [{"name": "recap", "summary": "..."}],
        },
    ])
    result = await dispatch.dispatch(
        "recap.imaginary_tool", {}, snap, live, None,
    )
    assert "error" in result
    assert "imaginary_tool" in result["error"]


async def test_dispatch_domain_op_no_pool_returns_placeholder(tmp_path: Path) -> None:
    """When pool=None, a valid domain op call returns a structured
    'not implemented' response. Lets Phase-1 consumers verify routing
    without spawning real backends."""
    snap, live = _setup(tmp_path, backends=[
        {
            "name": "recap",
            "command": ["node", "/x/index.js"],
            "cwd": "/x",
            "ops": [{"name": "recap", "summary": "..."}],
        },
    ])
    result = await dispatch.dispatch(
        "recap.recap", {"some": "arg"}, snap, live, None,
    )
    assert result.get("phase") == 1
    assert result["would_dispatch_to"]["backend"] == "recap"
    assert result["would_dispatch_to"]["tool"] == "recap"
    assert result["would_dispatch_to"]["args"] == {"some": "arg"}


async def test_dispatch_empty_operation_returns_error(tmp_path: Path) -> None:
    snap, live = _setup(tmp_path)
    result = await dispatch.dispatch("", None, snap, live, None)
    assert "error" in result


async def test_dispatch_non_string_operation_returns_error(tmp_path: Path) -> None:
    snap, live = _setup(tmp_path)
    result = await dispatch.dispatch(None, None, snap, live, None)  # type: ignore[arg-type]
    assert "error" in result
