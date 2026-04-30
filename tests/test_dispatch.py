"""Operation dispatch — meta-ops + domain-op routing."""
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


def test_dispatch_list_returns_catalog(tmp_path: Path) -> None:
    snap, live = _setup(tmp_path)
    result = dispatch.dispatch("list", None, snap, live)
    assert "ops" in result
    names = {op["name"] for op in result["ops"]}
    # All five meta-ops should be in the catalog the snapshot was created
    # with.
    assert {"list", "describe", "sync", "health", "manifest_version"} <= names


def test_dispatch_list_filters_by_namespace(tmp_path: Path) -> None:
    snap, live = _setup(tmp_path, snap_ops=[
        {"namespace": "meta",     "name": "list",        "summary": "..."},
        {"namespace": "recap",    "name": "recap.recap", "summary": "..."},
        {"namespace": "chatfork", "name": "chatfork.fork", "summary": "..."},
    ])
    result = dispatch.dispatch("list", {"namespace": "recap"}, snap, live)
    names = {op["name"] for op in result["ops"]}
    assert names == {"recap.recap"}


def test_dispatch_describe_known_op(tmp_path: Path) -> None:
    snap, live = _setup(tmp_path, snap_ops=[
        {"namespace": "recap", "name": "recap.recap", "summary": "Catch up on work"},
    ])
    result = dispatch.dispatch("describe", {"operation": "recap.recap"}, snap, live)
    assert result["name"] == "recap.recap"
    assert result["summary"] == "Catch up on work"
    assert "error" not in result


def test_dispatch_describe_missing_arg(tmp_path: Path) -> None:
    snap, live = _setup(tmp_path)
    result = dispatch.dispatch("describe", {}, snap, live)
    assert "error" in result
    assert "operation" in result["error"]


def test_dispatch_describe_unknown_op(tmp_path: Path) -> None:
    snap, live = _setup(tmp_path)
    result = dispatch.dispatch("describe", {"operation": "no.such.op"}, snap, live)
    assert "error" in result
    assert "unknown" in result["error"].lower()


def test_dispatch_sync_returns_diff(tmp_path: Path) -> None:
    snap, live = _setup(tmp_path, backends=[
        {
            "name": "recap",
            "command": ["node", "/x"],
            "ops": [{"name": "recap_new", "summary": "newly added"}],
        },
    ])
    result = dispatch.dispatch("sync", None, snap, live)
    # The snapshot doesn't list recap.recap_new but the live registry does
    # → drift.
    assert result["is_drifted"] is True
    assert any(op["name"] == "recap.recap_new" for op in result["added"])


def test_dispatch_health_lists_all_backends(tmp_path: Path) -> None:
    snap, live = _setup(tmp_path, backends=[
        {"name": "recap",     "command": ["node", "/r"], "ops": [{"name": "recap", "summary": "..."}]},
        {"name": "chatfork",  "command": ["node", "/c"], "ops": [{"name": "fork",  "summary": "..."}]},
    ])
    result = dispatch.dispatch("health", None, snap, live)
    names = {b["name"] for b in result["backends"]}
    assert names == {"recap", "chatfork"}
    # Phase 1: every backend reports not_connected since no pool wiring yet.
    assert all(b["status"] == "not_connected" for b in result["backends"])


def test_dispatch_manifest_version_returns_hash(tmp_path: Path) -> None:
    snap, live = _setup(tmp_path)
    result = dispatch.dispatch("manifest_version", None, snap, live)
    assert "snapshot_version" in result
    assert "snapshot_hash" in result
    assert result["snapshot_hash"].startswith("sha256:")


def test_dispatch_unknown_meta_op_falls_through_with_error(tmp_path: Path) -> None:
    """A name with no dot but not in META_OP_NAMES is genuinely unknown."""
    snap, live = _setup(tmp_path)
    result = dispatch.dispatch("frobnicate", None, snap, live)
    assert "error" in result
    assert "unknown op" in result["error"].lower()


def test_dispatch_domain_op_unknown_namespace(tmp_path: Path) -> None:
    snap, live = _setup(tmp_path)
    result = dispatch.dispatch("missing_backend.foo", None, snap, live)
    assert "error" in result
    assert "namespace" in result["error"].lower()


def test_dispatch_domain_op_known_namespace_unknown_tool(tmp_path: Path) -> None:
    """Backend declared, but the called tool isn't in its op list. That's
    a caller-error (not a backend error) — surface clearly."""
    snap, live = _setup(tmp_path, backends=[
        {
            "name": "recap",
            "command": ["node", "/x"],
            "ops": [{"name": "recap", "summary": "..."}],
        },
    ])
    result = dispatch.dispatch("recap.imaginary_tool", {}, snap, live)
    assert "error" in result
    assert "imaginary_tool" in result["error"]


def test_dispatch_domain_op_phase1_placeholder(tmp_path: Path) -> None:
    """In Phase 1, a valid domain op call returns a structured 'not yet
    wired' response. Phase 2 will replace this with real backend
    dispatch."""
    snap, live = _setup(tmp_path, backends=[
        {
            "name": "recap",
            "command": ["node", "/x/index.js"],
            "cwd": "/x",
            "ops": [{"name": "recap", "summary": "..."}],
        },
    ])
    result = dispatch.dispatch("recap.recap", {"some": "arg"}, snap, live)
    assert result.get("phase") == 1
    assert result["would_dispatch_to"]["backend"] == "recap"
    assert result["would_dispatch_to"]["tool"] == "recap"
    assert result["would_dispatch_to"]["args"] == {"some": "arg"}


def test_dispatch_empty_operation_returns_error(tmp_path: Path) -> None:
    snap, live = _setup(tmp_path)
    result = dispatch.dispatch("", None, snap, live)
    assert "error" in result


def test_dispatch_non_string_operation_returns_error(tmp_path: Path) -> None:
    snap, live = _setup(tmp_path)
    result = dispatch.dispatch(None, None, snap, live)  # type: ignore[arg-type]
    assert "error" in result
