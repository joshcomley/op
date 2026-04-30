"""Snapshot-vs-live drift computation."""
from __future__ import annotations

import json
from pathlib import Path

from op_gateway.diff import diff, expand_live_to_entries
from op_gateway.manifest import load_live, load_snapshot


def _write_snapshot(p: Path, ops: list[dict]) -> None:
    p.write_text(json.dumps({
        "snapshot_version": "0.0.1",
        "promoted_at": "2026-04-30T00:00:00Z",
        "hash": "sha256:placeholder",
        "highlights": [],
        "ops": ops,
    }), encoding="utf-8")


def _write_live(p: Path, backends: list[dict]) -> None:
    p.write_text(json.dumps({
        "registry_version": "1",
        "env": {},
        "backends": backends,
    }), encoding="utf-8")


def test_diff_no_drift_when_snapshot_matches_live(tmp_path: Path) -> None:
    snap_p = tmp_path / "snap.json"
    live_p = tmp_path / "live.json"
    _write_snapshot(snap_p, [
        {"namespace": "meta",  "name": "list",                  "summary": "Enumerate available ops"},
        {"namespace": "meta",  "name": "describe",              "summary": "Schema + docs for one op"},
        {"namespace": "meta",  "name": "sync",                  "summary": "Diff vs current live registry"},
        {"namespace": "meta",  "name": "health",                "summary": "Per-backend availability"},
        {"namespace": "meta",  "name": "manifest_version",      "summary": "Snapshot version + hash"},
        {"namespace": "recap", "name": "recap.recap",           "summary": "Catch up"},
    ])
    _write_live(live_p, [
        {
            "name": "recap",
            "command": ["node", "/x/index.js"],
            "ops": [{"name": "recap", "summary": "Catch up"}],
        },
    ])
    snap = load_snapshot(snap_p)
    live = load_live(live_p)
    result = diff(snap, live)
    assert result.is_drifted is False
    assert result.added == []
    assert result.removed == []


def test_diff_detects_added_op(tmp_path: Path) -> None:
    snap_p = tmp_path / "snap.json"
    live_p = tmp_path / "live.json"
    _write_snapshot(snap_p, [
        {"namespace": "meta",  "name": "list",                  "summary": "..."},
        {"namespace": "meta",  "name": "describe",              "summary": "..."},
        {"namespace": "meta",  "name": "sync",                  "summary": "..."},
        {"namespace": "meta",  "name": "health",                "summary": "..."},
        {"namespace": "meta",  "name": "manifest_version",      "summary": "..."},
        {"namespace": "recap", "name": "recap.recap",           "summary": "Catch up"},
    ])
    _write_live(live_p, [
        {
            "name": "recap",
            "command": ["node", "/x/index.js"],
            "ops": [
                {"name": "recap",     "summary": "Catch up"},
                {"name": "recap_all", "summary": "Multi-project recap"},
            ],
        },
    ])
    snap = load_snapshot(snap_p)
    live = load_live(live_p)
    result = diff(snap, live)
    assert result.is_drifted is True
    assert len(result.added) == 1
    assert result.added[0]["name"] == "recap.recap_all"
    assert result.added[0]["namespace"] == "recap"
    assert result.removed == []


def test_diff_detects_removed_op(tmp_path: Path) -> None:
    snap_p = tmp_path / "snap.json"
    live_p = tmp_path / "live.json"
    _write_snapshot(snap_p, [
        {"namespace": "meta",  "name": "list",             "summary": "..."},
        {"namespace": "meta",  "name": "describe",         "summary": "..."},
        {"namespace": "meta",  "name": "sync",             "summary": "..."},
        {"namespace": "meta",  "name": "health",           "summary": "..."},
        {"namespace": "meta",  "name": "manifest_version", "summary": "..."},
        {"namespace": "recap", "name": "recap.recap",      "summary": "Catch up"},
        {"namespace": "recap", "name": "recap.recap_old",  "summary": "Deprecated"},
    ])
    _write_live(live_p, [
        {
            "name": "recap",
            "command": ["node", "/x/index.js"],
            "ops": [{"name": "recap", "summary": "Catch up"}],
        },
    ])
    snap = load_snapshot(snap_p)
    live = load_live(live_p)
    result = diff(snap, live)
    assert result.is_drifted is True
    assert len(result.removed) == 1
    assert result.removed[0]["name"] == "recap.recap_old"


def test_expand_live_includes_meta_ops(tmp_path: Path) -> None:
    """Live expansion always includes the meta-ops, regardless of whether
    op.json declares them. The meta-ops are the gateway's own surface."""
    live_p = tmp_path / "live.json"
    _write_live(live_p, [])
    live = load_live(live_p)
    entries = expand_live_to_entries(live)
    names = {e.name for e in entries}
    assert {"list", "describe", "sync", "health", "manifest_version"} <= names


def test_diff_promote_hint_only_when_drifted(tmp_path: Path) -> None:
    """The 'run op promote' hint is shown only when there's drift —
    otherwise it'd be misleading prose."""
    snap_p = tmp_path / "snap.json"
    live_p = tmp_path / "live.json"
    _write_snapshot(snap_p, [
        {"namespace": "meta", "name": "list",             "summary": "..."},
        {"namespace": "meta", "name": "describe",         "summary": "..."},
        {"namespace": "meta", "name": "sync",             "summary": "..."},
        {"namespace": "meta", "name": "health",           "summary": "..."},
        {"namespace": "meta", "name": "manifest_version", "summary": "..."},
    ])
    _write_live(live_p, [])
    snap = load_snapshot(snap_p)
    live = load_live(live_p)
    result = diff(snap, live)
    d = result.to_dict()
    assert "promote_hint" not in d
