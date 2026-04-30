"""Diff a snapshot against the live registry.

Powers `op({operation: "sync"})` and the `op diff` CLI.

The snapshot is what the SDK currently sees (cached). The live registry
is what `op.json` declares right now. Drift comes in three forms:

  added            — ops in live that aren't in the snapshot
  removed          — ops in the snapshot that aren't in live
  changed_schemas  — ops in BOTH but whose `inputSchema` has drifted
                     since the snapshot was taken (Phase 3+)

The first two are name-level — they need only the snapshot + manifest.
`changed_schemas` requires comparing the snapshot's stored `schema_hash`
against the live backend's current `inputSchema` hash, which means the
live backend pool must be wired. Without a pool, schema-diff falls back
to "[]" and the result is name-only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .manifest import LiveManifest, Snapshot, SnapshotEntry, canonical_schema_hash


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


def diff(snapshot: Snapshot, live: LiveManifest, pool: Any | None = None) -> DiffResult:
    """Compute snapshot ↔ live drift.

    `pool` is an optional BackendPool (typed Any here to avoid the
    import cycle). When wired, the diff includes `changed_schemas` —
    ops present in both snapshot and live whose `inputSchema` has
    drifted since the snapshot was taken. Without a pool, only name-
    level adds/removes are reported.
    """
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

    # Schema drift detection. Compare each shared op's snapshot
    # schema_hash against the backend's current schema_hash (computed
    # from the live tools/list cache via the pool's find_tool API).
    changed_schemas = _detect_schema_drift(snap_by_name, live_by_name, pool)

    return DiffResult(
        snapshot_version=snapshot.snapshot_version,
        snapshot_hash=snapshot.hash,
        live_state_hash=live_hash,
        is_drifted=bool(added or removed or changed_schemas),
        added=added,
        removed=removed,
        changed_schemas=changed_schemas,
    )


def _detect_schema_drift(
    snap_by_name: dict[str, SnapshotEntry],
    live_by_name: dict[str, SnapshotEntry],
    pool: Any | None,
) -> list[dict[str, Any]]:
    """For each op in BOTH the snapshot and the live manifest, compare
    the snapshot's schema_hash to the backend's current schema (via
    the pool's cached tools/list).

    Returns one entry per op where the hashes differ. Skips:
      * meta-ops (no backend schema to compare)
      * ops whose snapshot has no schema_hash (legacy / unreachable
        at promote time)
      * ops whose backend isn't currently reachable (the pool's
        find_tool returns None)
      * everything when no pool is wired (Phase 1-style standalone)
    """
    if pool is None:
        return []
    shared_names = set(snap_by_name) & set(live_by_name)
    out: list[dict[str, Any]] = []
    for name in sorted(shared_names):
        snap_entry = snap_by_name[name]
        if snap_entry.namespace == "meta":
            continue
        if not snap_entry.schema_hash:
            # Legacy snapshot or unreachable-at-promote-time. Skip rather
            # than spuriously flagging every op as "changed".
            continue
        # Strip namespace prefix to get the backend's view of the name.
        if "." not in name:
            continue
        _, _, tool_name = name.partition(".")
        live_tool = pool.find_tool(snap_entry.namespace, tool_name)
        if live_tool is None:
            # Backend unreachable or doesn't expose this tool right now.
            # The agent will see this as part of the `health` op anyway;
            # don't double-report under changed_schemas.
            continue
        current_schema = getattr(live_tool, "inputSchema", None)
        current_hash = canonical_schema_hash(current_schema)
        if current_hash != snap_entry.schema_hash:
            out.append({
                "name":           name,
                "namespace":      snap_entry.namespace,
                "snapshot_hash":  snap_entry.schema_hash,
                "current_hash":   current_hash,
                "hint":           "Schema drifted since this snapshot was "
                                  "promoted. Call op({operation: \"describe\", "
                                  f"args: {{operation: \"{name}\"}}}}) for "
                                  "the current schema.",
            })
    return out


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
