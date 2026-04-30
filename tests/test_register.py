"""`op register` CLI — wire op into ~/.claude.json."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from op_cli.register import run as register_run


def test_register_creates_target_when_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "claude.json"
    assert not target.exists()
    rc = register_run(["--target", str(target), "--cwd", str(tmp_path)])
    assert rc == 0
    assert target.exists()
    config = json.loads(target.read_text(encoding="utf-8"))
    assert "op" in config["mcpServers"]
    entry = config["mcpServers"]["op"]
    assert entry["command"] == sys.executable
    assert entry["args"] == ["-m", "op_gateway.server"]
    assert entry["cwd"] == str(tmp_path)


def test_register_adds_to_existing_target_preserving_other_servers(
    tmp_path: Path,
) -> None:
    target = tmp_path / "claude.json"
    target.write_text(json.dumps({
        "mcpServers": {
            "other-mcp": {
                "command": "node",
                "args": ["/some/script.mjs"],
            },
        },
        "someOtherKey": {"keep": "me"},
    }), encoding="utf-8")
    rc = register_run([
        "--target", str(target),
        "--cwd", str(tmp_path),
        "--no-backup",
    ])
    assert rc == 0
    config = json.loads(target.read_text(encoding="utf-8"))
    assert "op" in config["mcpServers"]
    assert "other-mcp" in config["mcpServers"]   # untouched
    assert config["someOtherKey"] == {"keep": "me"}  # untouched


def test_register_updates_existing_op_entry(tmp_path: Path) -> None:
    target = tmp_path / "claude.json"
    target.write_text(json.dumps({
        "mcpServers": {
            "op": {
                "command": "old-python",
                "args": ["-m", "op_gateway.server"],
                "cwd": "/old/path",
            },
        },
    }), encoding="utf-8")
    rc = register_run([
        "--target", str(target),
        "--cwd", str(tmp_path / "new"),
        "--python", "new-python",
        "--no-backup",
    ])
    assert rc == 0
    config = json.loads(target.read_text(encoding="utf-8"))
    entry = config["mcpServers"]["op"]
    assert entry["command"] == "new-python"
    assert entry["cwd"] == str(tmp_path / "new")


def test_register_no_op_when_already_registered(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Re-running register with the same args is a clean no-op."""
    target = tmp_path / "claude.json"
    register_run(["--target", str(target), "--cwd", str(tmp_path), "--no-backup"])
    capsys.readouterr()  # discard first-run output
    rc = register_run(["--target", str(target), "--cwd", str(tmp_path), "--no-backup"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no change" in out.lower() or "already registered" in out.lower()


def test_register_remove_takes_op_out(tmp_path: Path) -> None:
    target = tmp_path / "claude.json"
    target.write_text(json.dumps({
        "mcpServers": {
            "op":         {"command": "x", "args": [], "cwd": "/y"},
            "other-mcp":  {"command": "y", "args": []},
        },
    }), encoding="utf-8")
    rc = register_run(["--target", str(target), "--remove", "--no-backup"])
    assert rc == 0
    config = json.loads(target.read_text(encoding="utf-8"))
    assert "op" not in config["mcpServers"]
    assert "other-mcp" in config["mcpServers"]


def test_register_remove_no_op_when_absent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "claude.json"
    target.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    rc = register_run(["--target", str(target), "--remove"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "nothing to do" in out.lower() or "not registered" in out.lower()


def test_register_print_only_does_not_write(tmp_path: Path) -> None:
    """`--print-only` shows the JSON without touching the file."""
    target = tmp_path / "claude.json"
    rc = register_run([
        "--target", str(target), "--cwd", str(tmp_path), "--print-only",
    ])
    assert rc == 0
    assert not target.exists()


def test_register_creates_backup_by_default(tmp_path: Path) -> None:
    target = tmp_path / "claude.json"
    original_content = json.dumps({"mcpServers": {"x": {}}})
    target.write_text(original_content, encoding="utf-8")
    rc = register_run(["--target", str(target), "--cwd", str(tmp_path)])
    assert rc == 0
    backups = list(tmp_path.glob("claude.json.bak.*"))
    assert len(backups) == 1
    # The backup contains the ORIGINAL content (pre-edit).
    assert backups[0].read_text(encoding="utf-8") == original_content


def test_register_rejects_malformed_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """If the target is malformed JSON, register exits non-zero rather
    than silently corrupting the file."""
    target = tmp_path / "claude.json"
    target.write_text("{ this is not valid json", encoding="utf-8")
    rc = register_run(["--target", str(target), "--cwd", str(tmp_path)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "parse" in err.lower() or "failed" in err.lower()
