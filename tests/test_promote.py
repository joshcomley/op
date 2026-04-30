"""op promote — regenerate snapshot from live manifest."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from op_cli.promote import _next_version, run as promote_run
from op_gateway.manifest import load_snapshot


def _write_live(p: Path, backends: list[dict]) -> Path:
    p.write_text(json.dumps({
        "registry_version": "1",
        "env": {},
        "backends": backends,
    }), encoding="utf-8")
    return p


def test_next_version_increments_patch() -> None:
    assert _next_version("1.2.3") == "1.2.4"
    assert _next_version("0.0.1") == "0.0.2"
    assert _next_version("1.2") == "1.2.1"
    assert _next_version("1") == "1.0.1"


def test_next_version_handles_no_prior() -> None:
    assert _next_version(None) == "0.0.1"


def test_next_version_handles_unparseable() -> None:
    """An unparseable version string just gets a `.1` appended. Good enough
    for the rare 'someone wrote a freeform tag' case."""
    assert _next_version("custom-tag") == "custom-tag.1"


def test_promote_creates_initial_snapshot(
    isolated_op_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    _write_live(isolated_op_home / "op.json", [
        {
            "name": "recap",
            "command": ["node", "/x/index.js"],
            "ops": [
                {"name": "recap", "summary": "Catch up"},
                {"name": "recap_all", "summary": "Multi-project"},
            ],
        },
    ])
    rc = promote_run([])
    assert rc == 0

    snap_path = isolated_op_home / "op.snapshot.json"
    assert snap_path.exists()
    snap = load_snapshot(snap_path)
    assert snap.snapshot_version == "0.0.1"
    assert snap.hash.startswith("sha256:")

    op_names = {op.name for op in snap.ops}
    assert "list" in op_names                # meta-op always present
    assert "recap.recap" in op_names         # backend op flattened with namespace
    assert "recap.recap_all" in op_names


def test_promote_increments_version_on_subsequent_runs(
    isolated_op_home: Path,
) -> None:
    _write_live(isolated_op_home / "op.json", [])
    promote_run([])
    snap1 = load_snapshot(isolated_op_home / "op.snapshot.json")

    promote_run([])
    snap2 = load_snapshot(isolated_op_home / "op.snapshot.json")

    assert snap1.snapshot_version != snap2.snapshot_version
    assert snap1.hash == snap2.hash    # content unchanged → same hash


def test_promote_explicit_version(isolated_op_home: Path) -> None:
    _write_live(isolated_op_home / "op.json", [])
    rc = promote_run(["--version", "2.0.0"])
    assert rc == 0
    snap = load_snapshot(isolated_op_home / "op.snapshot.json")
    assert snap.snapshot_version == "2.0.0"


def test_promote_dry_run_does_not_write(isolated_op_home: Path) -> None:
    _write_live(isolated_op_home / "op.json", [])
    rc = promote_run(["--dry-run"])
    assert rc == 0
    assert not (isolated_op_home / "op.snapshot.json").exists()


def test_promote_fails_when_live_missing(
    isolated_op_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    rc = promote_run([])
    assert rc == 1
    err = capsys.readouterr().err
    assert "op.json" in err


def test_promote_prunes_invalid_highlights(
    isolated_op_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """A highlight that names an op no longer in the live registry must
    be silently pruned — better than shipping a snapshot that points at
    nothing."""
    # First, create a snapshot that has a highlight pointing at recap.recap
    snap_path = isolated_op_home / "op.snapshot.json"
    snap_path.write_text(json.dumps({
        "snapshot_version": "0.0.1",
        "promoted_at": "2026-04-30T00:00:00Z",
        "hash": "sha256:existing",
        "highlights": [{"name": "recap.recap"}, {"name": "recap.gone"}],
        "ops": [
            {"namespace": "recap", "name": "recap.recap", "summary": "..."},
        ],
    }), encoding="utf-8")

    # Now write live with NO recap backend. recap.recap and recap.gone
    # both go away from live.
    _write_live(isolated_op_home / "op.json", [])

    rc = promote_run([])
    assert rc == 0

    new_snap = load_snapshot(snap_path)
    # Both highlights pruned — neither is in live anymore.
    assert new_snap.highlights == ()


def test_promote_no_probe_omits_schema_hashes(isolated_op_home: Path) -> None:
    """`--no-probe` skips backend probing; resulting snapshot has no
    schema_hash on any op. Useful for tests + fast iteration when
    you don't want to spawn every backend."""
    import sys
    from pathlib import Path as _Path
    fake = _Path(__file__).parent / "fake_backend_mcp.py"
    _write_live(isolated_op_home / "op.json", [
        {
            "name": "fake",
            "command": [sys.executable, str(fake)],
            "ops": [{"name": "echo", "summary": "Echo"}],
        },
    ])
    rc = promote_run(["--no-probe"])
    assert rc == 0
    snap = load_snapshot(isolated_op_home / "op.snapshot.json")
    # Every op in the snapshot — meta-ops AND the fake.echo entry —
    # should have schema_hash=None.
    for entry in snap.ops:
        assert entry.schema_hash is None


def test_promote_with_probe_writes_schema_hashes(isolated_op_home: Path) -> None:
    """Default promote (probe enabled) connects to each backend, fetches
    tools/list, computes a hash per tool's inputSchema, and writes it
    into the snapshot. Without that hash, schema-drift detection in
    `sync` can't work."""
    import sys
    from pathlib import Path as _Path
    fake = _Path(__file__).parent / "fake_backend_mcp.py"
    _write_live(isolated_op_home / "op.json", [
        {
            "name": "fake",
            "command": [sys.executable, str(fake)],
            "ops": [
                {"name": "echo", "summary": "Echo"},
                {"name": "fail", "summary": "Always errors"},
            ],
        },
    ])
    rc = promote_run([])
    assert rc == 0
    snap = load_snapshot(isolated_op_home / "op.snapshot.json")

    # Domain ops should have a schema_hash; meta-ops should not.
    by_name = {e.name: e for e in snap.ops}
    assert by_name["fake.echo"].schema_hash is not None
    assert by_name["fake.echo"].schema_hash.startswith("sha256:")
    assert by_name["fake.fail"].schema_hash is not None
    assert by_name["list"].schema_hash is None
    assert by_name["describe"].schema_hash is None


def test_promote_unreachable_backend_skips_schema_hash(
    isolated_op_home: Path,
) -> None:
    """A backend whose command is invalid (immediate exit / nonexistent
    binary) shouldn't crash promote — it should just write the
    snapshot entries without schema_hash for that backend's ops, so
    the user can fix the backend later and re-promote."""
    import sys
    _write_live(isolated_op_home / "op.json", [
        {
            "name": "broken",
            "command": [sys.executable, "-c", "import sys; sys.exit(1)"],
            "ops": [{"name": "phantom", "summary": "Doesn't exist"}],
        },
    ])
    rc = promote_run(["--probe-timeout", "1.0"])
    assert rc == 0
    snap = load_snapshot(isolated_op_home / "op.snapshot.json")
    by_name = {e.name: e for e in snap.ops}
    # The broken.phantom entry should still be in the snapshot (it's
    # what op.json declares) — just without a schema_hash.
    assert "broken.phantom" in by_name
    assert by_name["broken.phantom"].schema_hash is None


def test_promote_keeps_valid_highlights(isolated_op_home: Path) -> None:
    snap_path = isolated_op_home / "op.snapshot.json"
    snap_path.write_text(json.dumps({
        "snapshot_version": "0.0.1",
        "promoted_at": "2026-04-30T00:00:00Z",
        "hash": "sha256:existing",
        "highlights": [{"name": "recap.recap"}],
        "ops": [
            {"namespace": "recap", "name": "recap.recap", "summary": "..."},
        ],
    }), encoding="utf-8")

    _write_live(isolated_op_home / "op.json", [
        {
            "name": "recap",
            "command": ["node", "/x"],
            "ops": [{"name": "recap", "summary": "..."}],
        },
    ])

    rc = promote_run([])
    assert rc == 0

    new_snap = load_snapshot(snap_path)
    assert "recap.recap" in new_snap.highlights
