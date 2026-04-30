"""Diff a snapshot against the live registry. Powers `op({operation: "sync"})`
and the `op diff` CLI.

The snapshot is what the SDK currently sees (cached). The live registry
is what `op.json` declares right now. Drift is the delta between them —
ops added to live since the last promote, ops removed from live since
the last promote, and (eventually) ops whose schemas changed.

Phase 1 only computes name-level adds/removes. Schema-diff is Phase 3
once we have backend `tools/list` data flowing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .manifest import LiveManifest, Snapshot, SnapshotEntry


@dataclass
class DiffResult:
    snapshot_version: str
    snapshot_hash: str
    live_state_hash: str
    is_drifted: bool
    added: list[dict[str, Any]] = field(default_factory=list)
    removed: list[dict[str, Any]] = field(default_factory=list)
    changed_schemas: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "snapshot_version": self.snapshot_version,
            "snapshot_hash":    self.snapshot_hash,
            "live_state_hash":  self.live_state_hash,
            "is_drifted":       self.is_drifted,
            "added":            self.added,
            "removed":          self.removed,
            "changed_schemas":  self.changed_schemas,
        }
        if self.is_drifted:
            d["promote_hint"] = (
                "Run `op promote` from any shell to make these visible "
                "across sessions. One cache miss next session, then stable."
            )
        return d


def expand_live_to_entries(live: LiveManifest) -> tuple[SnapshotEntry, ...]:
    """Flatten the live manifest into the same shape as snapshot.ops.

    Used both to compute the hash-of-live-state and to compute the diff
    against an existing snapshot. Includes the meta-ops because they're
    always part of the catalog the agent sees."""
    entries: list[SnapshotEntry] = list(_meta_entries())
    for backend in live.backends:
        for op in backend.ops:
            entries.append(SnapshotEntry(
                namespace=backend.name,
                name=f"{backend.name}.{op.name}",
                summary=op.summary,
            ))
    return tuple(entries)


def diff(snapshot: Snapshot, live: LiveManifest) -> DiffResult:
    """Compute snapshot ↔ live drift."""
    from .manifest import canonical_hash

    live_entries = expand_live_to_entries(live)
    live_hash = canonical_hash(snapshot.highlights, live_entries)

    snap_by_name = {op.name: op for op in snapshot.ops}
    live_by_name = {op.name: op for op in live_entries}

    added_names   = sorted(set(live_by_name) - set(snap_by_name))
    removed_names = sorted(set(snap_by_name) - set(live_by_name))

    added = [
        {
            "name": name,
            "namespace": live_by_name[name].namespace,
            "summary": live_by_name[name].summary,
        }
        for name in added_names
    ]
    removed = [
        {
            "name": name,
            "namespace": snap_by_name[name].namespace,
        }
        for name in removed_names
    ]

    return DiffResult(
        snapshot_version=snapshot.snapshot_version,
        snapshot_hash=snapshot.hash,
        live_state_hash=live_hash,
        is_drifted=bool(added or removed),
        added=added,
        removed=removed,
        changed_schemas=[],   # Phase 3
    )


def _meta_entries() -> tuple[SnapshotEntry, ...]:
    """The meta-op catalog the gateway always exposes. Kept in sync with
    the gateway's own dispatch table; tests assert that."""
    return (
        SnapshotEntry("meta", "list",             "Enumerate available ops"),
        SnapshotEntry("meta", "describe",         "Schema + docs for one op"),
        SnapshotEntry("meta", "sync",             "Diff vs current live registry"),
        SnapshotEntry("meta", "health",           "Per-backend availability"),
        SnapshotEntry("meta", "manifest_version", "Snapshot version + hash"),
    )
