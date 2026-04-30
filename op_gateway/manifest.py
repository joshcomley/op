"""Loaders + canonicalisers for op.json (live) and op.snapshot.json (frozen).

The two files have the same overall shape but different roles. The live
manifest is what you edit; the snapshot is the cache-stable view that
gets promoted from the live manifest at your discretion.

Canonicalisation matters because the snapshot's `hash` is derived from
sorted, normalized JSON — so byte-equivalent snapshots compare equal
even if you reorder keys in the file by hand.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_INTERPOLATE_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


@dataclass(frozen=True)
class OpDef:
    """One op in a backend's catalog."""
    name: str       # e.g. "recap_status"
    summary: str    # short human description

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "summary": self.summary}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "OpDef":
        return cls(name=str(d["name"]), summary=str(d.get("summary", "")))


@dataclass(frozen=True)
class BackendDef:
    """One backend MCP server registered in op.json."""
    name: str
    command: tuple[str, ...]
    cwd: str | None
    env: dict[str, str]
    ops: tuple[OpDef, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "command": list(self.command),
            **({"cwd": self.cwd} if self.cwd else {}),
            **({"env": self.env} if self.env else {}),
            "ops": [op.to_dict() for op in self.ops],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BackendDef":
        return cls(
            name=str(d["name"]),
            command=tuple(str(x) for x in d.get("command", [])),
            cwd=str(d["cwd"]) if d.get("cwd") else None,
            env={str(k): str(v) for k, v in (d.get("env") or {}).items()},
            ops=tuple(OpDef.from_dict(o) for o in d.get("ops", [])),
        )


@dataclass(frozen=True)
class LiveManifest:
    """Parsed `op.json`. Everything you can edit.

    `interpolation_env` is the merged `{env block from op.json, then os.environ}`.
    Used to resolve `${VAR}` placeholders in `command` / `cwd` / per-backend
    `env` values.
    """
    registry_version: str
    interpolation_env: dict[str, str]
    backends: tuple[BackendDef, ...]

    def backend_by_name(self, name: str) -> BackendDef | None:
        for b in self.backends:
            if b.name == name:
                return b
        return None


@dataclass(frozen=True)
class SnapshotEntry:
    namespace: str          # "meta" | backend name
    name: str               # full namespaced name, e.g. "recap.recap_all"
    summary: str
    # Hash of the backend tool's inputSchema at promote time. Populated
    # by `op promote` for domain ops by probing the backend's live
    # tools/list. Snapshot entries without a hash (legacy snapshots,
    # meta-ops, or backends that weren't reachable at promote time) carry
    # None — the diff machinery skips schema comparison for those.
    schema_hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "namespace": self.namespace,
            "name":      self.name,
            "summary":   self.summary,
        }
        if self.schema_hash is not None:
            d["schema_hash"] = self.schema_hash
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SnapshotEntry":
        sh = d.get("schema_hash")
        return cls(
            namespace=str(d["namespace"]),
            name=str(d["name"]),
            summary=str(d.get("summary", "")),
            schema_hash=str(sh) if sh else None,
        )


@dataclass(frozen=True)
class Snapshot:
    """Parsed `op.snapshot.json`. Cache-stable view."""
    snapshot_version: str
    promoted_at: str        # ISO 8601
    hash: str               # "sha256:<hex>"
    highlights: tuple[str, ...]   # op names featured in the description
    ops: tuple[SnapshotEntry, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_version": self.snapshot_version,
            "promoted_at": self.promoted_at,
            "hash": self.hash,
            "highlights": [{"name": n} for n in self.highlights],
            "ops": [op.to_dict() for op in self.ops],
        }


def load_live(path: Path) -> LiveManifest:
    """Parse op.json. Builds the interpolation environment by overlaying
    op.json's `env` block on top of `os.environ`."""
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    env_block = {str(k): str(v) for k, v in (data.get("env") or {}).items()}
    interp_env = {**os.environ, **env_block}

    raw_backends = data.get("backends") or []
    backends = tuple(
        _interpolate_backend(BackendDef.from_dict(b), interp_env)
        for b in raw_backends
    )
    return LiveManifest(
        registry_version=str(data.get("registry_version", "1")),
        interpolation_env=interp_env,
        backends=backends,
    )


def load_snapshot(path: Path) -> Snapshot:
    """Parse op.snapshot.json."""
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    highlights_raw = data.get("highlights") or []
    highlights = tuple(
        # accept both the canonical form `[{name: "..."}]` and the loose form `["..."]`
        h["name"] if isinstance(h, dict) else str(h)
        for h in highlights_raw
    )
    ops = tuple(SnapshotEntry.from_dict(o) for o in data.get("ops") or [])
    return Snapshot(
        snapshot_version=str(data["snapshot_version"]),
        promoted_at=str(data["promoted_at"]),
        hash=str(data["hash"]),
        highlights=highlights,
        ops=ops,
    )


def _interpolate(value: str, env: dict[str, str]) -> str:
    """Replace `${VAR}` placeholders in `value` from `env`. Unknown vars
    are left literal so missing config produces obviously-broken paths
    rather than silent emptiness."""
    def _sub(m: re.Match[str]) -> str:
        var = m.group(1)
        return env.get(var, m.group(0))
    return _INTERPOLATE_RE.sub(_sub, value)


def _interpolate_backend(b: BackendDef, env: dict[str, str]) -> BackendDef:
    """Apply ${VAR} interpolation to a backend's command/cwd/env."""
    new_command = tuple(_interpolate(x, env) for x in b.command)
    new_cwd = _interpolate(b.cwd, env) if b.cwd else None
    new_env = {k: _interpolate(v, env) for k, v in b.env.items()}
    return BackendDef(
        name=b.name,
        command=new_command,
        cwd=new_cwd,
        env=new_env,
        ops=b.ops,
    )


def canonical_hash(highlights: tuple[str, ...], ops: tuple[SnapshotEntry, ...]) -> str:
    """Compute a stable hash of the snapshot's catalog content.

    Includes only the parts that affect the cache key — highlights and op
    catalog. Excludes `snapshot_version`, `promoted_at`, and the hash
    itself, so the hash is derivable from the rest.

    Sort + canonical-JSON-serialize so byte-equivalent snapshots produce
    equal hashes regardless of key order or whitespace."""
    payload = {
        "highlights": sorted(highlights),
        "ops": sorted(
            (op.to_dict() for op in ops),
            key=lambda o: (o["namespace"], o["name"]),
        ),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def canonical_schema_hash(schema: Any) -> str:
    """Hash of one tool's `inputSchema`. Used to detect when a backend's
    tool argument shape has drifted from what was captured at promote
    time, so the agent can be told via `op({operation: "sync"})`.

    Canonicalises by sort_keys to avoid spurious mismatches from JSON
    key-order differences. None / empty schemas hash to a sentinel so
    they compare equal to other empties."""
    if schema is None:
        schema = {}
    canonical = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
