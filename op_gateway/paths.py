"""Path resolution for op's runtime files.

Two files matter:

* `op.json` — the LIVE registry. Edit freely; no cache cost.
* `op.snapshot.json` — the FROZEN snapshot. What the SDK currently sees.
  Regenerated only via `op promote`.

By default both live next to op's install dir. Override via the
`OP_HOME` env var (mostly for tests).
"""
from __future__ import annotations

import os
from pathlib import Path


def op_home() -> Path:
    """Directory where op.json + op.snapshot.json live.

    Production default: `C:\\D\\op\\` on Windows, or the directory
    containing this file's parent on other platforms.

    Override via `OP_HOME` env var.
    """
    override = os.environ.get("OP_HOME")
    if override:
        return Path(override)
    if os.name == "nt":
        candidate = Path(r"C:\D\op")
        if candidate.exists():
            return candidate
    # Fallback: the directory containing the package's parent (= repo root)
    return Path(__file__).resolve().parent.parent


def live_manifest_path() -> Path:
    """Path to the LIVE manifest (`op.json`)."""
    return op_home() / "op.json"


def snapshot_path() -> Path:
    """Path to the FROZEN snapshot (`op.snapshot.json`)."""
    return op_home() / "op.snapshot.json"
