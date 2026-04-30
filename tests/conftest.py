"""Shared pytest fixtures.

Every test runs with `OP_HOME` pointed at a tmp dir, so production
config at `C:\\D\\op\\` is never read or written by the test suite."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_op_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point op's path-resolver at a per-test tmp dir."""
    monkeypatch.setenv("OP_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def empty_live_json(isolated_op_home: Path) -> Path:
    """An empty op.json (no backends). Lets meta-ops work; domain ops
    return 'unknown namespace'."""
    p = isolated_op_home / "op.json"
    p.write_text(json.dumps({
        "registry_version": "1",
        "env": {},
        "backends": [],
    }), encoding="utf-8")
    return p


@pytest.fixture
def empty_snapshot_json(isolated_op_home: Path) -> Path:
    """A minimal snapshot containing only the meta-ops."""
    p = isolated_op_home / "op.snapshot.json"
    p.write_text(json.dumps({
        "snapshot_version": "0.0.1",
        "promoted_at": "2026-04-30T00:00:00Z",
        "hash": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
        "highlights": [],
        "ops": [
            {"namespace": "meta", "name": "list", "summary": "Enumerate available ops"},
            {"namespace": "meta", "name": "describe", "summary": "Schema + docs for one op"},
            {"namespace": "meta", "name": "sync", "summary": "Diff vs current live registry"},
            {"namespace": "meta", "name": "health", "summary": "Per-backend availability"},
            {"namespace": "meta", "name": "manifest_version", "summary": "Snapshot version + hash"},
        ],
    }), encoding="utf-8")
    return p
