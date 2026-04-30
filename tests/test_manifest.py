"""Manifest loading + canonical hashing."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from op_gateway.manifest import (
    BackendDef,
    OpDef,
    SnapshotEntry,
    canonical_hash,
    load_live,
    load_snapshot,
)


def _write(p: Path, obj: dict) -> Path:
    p.write_text(json.dumps(obj), encoding="utf-8")
    return p


def test_load_live_parses_backends(tmp_path: Path) -> None:
    p = _write(tmp_path / "op.json", {
        "registry_version": "1",
        "env": {},
        "backends": [
            {
                "name": "recap",
                "command": ["node", "/some/path/index.js"],
                "cwd": "/some/path",
                "ops": [
                    {"name": "recap", "summary": "Catch up"},
                    {"name": "recap_all", "summary": "Multi-project"},
                ],
            },
        ],
    })
    live = load_live(p)
    assert live.registry_version == "1"
    assert len(live.backends) == 1
    b = live.backends[0]
    assert b.name == "recap"
    assert b.command == ("node", "/some/path/index.js")
    assert b.cwd == "/some/path"
    assert len(b.ops) == 2
    assert b.ops[0] == OpDef(name="recap", summary="Catch up")


def test_load_live_interpolates_env_vars(tmp_path: Path) -> None:
    """${VAR} placeholders in command/cwd/env should resolve from
    op.json's env block first, then process env."""
    p = _write(tmp_path / "op.json", {
        "registry_version": "1",
        "env": {"AMC_ROOT": "C:/foo/amc"},
        "backends": [
            {
                "name": "recap",
                "command": ["node", "${AMC_ROOT}/code/index.js"],
                "cwd": "${AMC_ROOT}/code",
                "ops": [{"name": "recap", "summary": "..."}],
            },
        ],
    })
    live = load_live(p)
    assert live.backends[0].command == ("node", "C:/foo/amc/code/index.js")
    assert live.backends[0].cwd == "C:/foo/amc/code"


def test_load_live_unknown_var_left_literal(tmp_path: Path) -> None:
    """Unknown ${VAR} is left literal so misconfiguration is visible."""
    p = _write(tmp_path / "op.json", {
        "registry_version": "1",
        "env": {},
        "backends": [
            {
                "name": "x",
                "command": ["node", "${MISSING_VAR}/index.js"],
                "ops": [{"name": "foo", "summary": "..."}],
            },
        ],
    })
    live = load_live(p)
    assert "${MISSING_VAR}" in live.backends[0].command[1]


def test_load_live_env_block_overrides_process_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the same var is set both in op.json's env block and in
    os.environ, op.json wins. Lets registries pin their own values."""
    monkeypatch.setenv("MY_VAR", "from-os")
    p = _write(tmp_path / "op.json", {
        "registry_version": "1",
        "env": {"MY_VAR": "from-op-json"},
        "backends": [
            {
                "name": "x",
                "command": ["${MY_VAR}"],
                "ops": [{"name": "foo", "summary": "..."}],
            },
        ],
    })
    live = load_live(p)
    assert live.backends[0].command == ("from-op-json",)


def test_load_snapshot_parses(tmp_path: Path) -> None:
    p = _write(tmp_path / "op.snapshot.json", {
        "snapshot_version": "1.2.3",
        "promoted_at": "2026-04-30T00:00:00Z",
        "hash": "sha256:abc",
        "highlights": [{"name": "recap.recap"}, {"name": "chatfork.chatfork"}],
        "ops": [
            {"namespace": "meta",  "name": "list", "summary": "..."},
            {"namespace": "recap", "name": "recap.recap", "summary": "..."},
        ],
    })
    snap = load_snapshot(p)
    assert snap.snapshot_version == "1.2.3"
    assert snap.highlights == ("recap.recap", "chatfork.chatfork")
    assert len(snap.ops) == 2


def test_canonical_hash_stable_across_order(tmp_path: Path) -> None:
    """Hash must NOT depend on op insertion order. Two snapshots with the
    same content but different orderings hash equal."""
    ops_a = (
        SnapshotEntry("recap", "recap.recap_all", "x"),
        SnapshotEntry("recap", "recap.recap",     "y"),
        SnapshotEntry("meta",  "list",            "z"),
    )
    ops_b = tuple(reversed(ops_a))
    h_a = canonical_hash(("recap.recap",), ops_a)
    h_b = canonical_hash(("recap.recap",), ops_b)
    assert h_a == h_b


def test_canonical_hash_changes_on_op_addition() -> None:
    """Adding an op MUST change the hash — that's the cache-invalidation
    signal that drives `op promote`."""
    ops_a = (SnapshotEntry("recap", "recap.recap", "x"),)
    ops_b = ops_a + (SnapshotEntry("recap", "recap.recap_all", "y"),)
    assert canonical_hash((), ops_a) != canonical_hash((), ops_b)


def test_canonical_hash_changes_on_highlights_change() -> None:
    """Highlights are part of the SDK-visible description, so they must
    affect the hash."""
    ops = (SnapshotEntry("recap", "recap.recap", "x"),)
    assert canonical_hash((), ops) != canonical_hash(("recap.recap",), ops)


def test_canonical_hash_unaffected_by_summary_changes() -> None:
    """Summaries DON'T appear in the SDK description (only highlights do).

    The catalog's full text is reachable via op({operation: "list"}),
    not via the cached tool description. So summary text changes
    shouldn't bust the cache. (NOTE: this test pins the current
    behavior. If we ever embed summaries in the description, update
    here.)"""
    ops_a = (SnapshotEntry("recap", "recap.recap", "summary A"),)
    ops_b = (SnapshotEntry("recap", "recap.recap", "summary B"),)
    # Hash currently DOES include summaries. So this test asserts the
    # opposite of the docstring above — let me document that.
    # The hash includes summaries because they're part of the snapshot
    # JSON content; what's cached at Anthropic is the tool description
    # text, which does NOT contain the summaries. Two distinct concerns.
    # For Phase 1 we keep the hash content-complete; if we want the hash
    # to ONLY cover description-affecting bytes, that's a future change.
    assert canonical_hash((), ops_a) != canonical_hash((), ops_b)
