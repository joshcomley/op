"""`op promote` — regenerate op.snapshot.json from op.json.

Reads the live manifest, expands it (including meta-ops), computes a
canonical hash, bumps the version, writes the new snapshot. Doesn't
touch any running gateway processes — the next gateway spawn picks up
the new snapshot.

Idempotent on a clean tree: running promote twice in a row produces
two snapshots with the same content but different timestamps. The hash
identifies content equality.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from op_gateway import paths
from op_gateway.diff import expand_live_to_entries
from op_gateway.manifest import (
    Snapshot,
    SnapshotEntry,
    canonical_hash,
    load_live,
    load_snapshot,
)
from op_gateway.probe import hash_lookup, probe_backends_sync


DEFAULT_HIGHLIGHTS: tuple[str, ...] = ()  # User curates after promote.


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="op promote")
    parser.add_argument(
        "--version",
        help="Explicit snapshot_version string. Defaults to auto-incrementing "
             "the patch component of the existing snapshot's version.",
    )
    parser.add_argument(
        "--keep-highlights",
        action="store_true",
        help="Carry highlights from the existing snapshot into the new one. "
             "Default is to keep them — pass --no-keep-highlights to clear.",
        default=True,
    )
    parser.add_argument(
        "--no-keep-highlights",
        dest="keep_highlights",
        action="store_false",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change without writing the snapshot file.",
    )
    parser.add_argument(
        "--no-probe",
        action="store_true",
        help="Skip probing each backend for live schemas. Faster but the "
             "snapshot ships without schema_hash, so future `sync` calls "
             "can't detect schema drift on those ops. Useful for tests "
             "or when backends aren't reachable from the CLI environment.",
    )
    parser.add_argument(
        "--probe-timeout",
        type=float,
        default=10.0,
        help="Seconds to wait for each backend to reach `up` during probe "
             "(default: 10.0). Backends that don't connect in time get "
             "no schema_hash for their ops.",
    )
    args = parser.parse_args(argv)

    live_path = paths.live_manifest_path()
    snap_path = paths.snapshot_path()

    if not live_path.exists():
        print(f"error: {live_path} does not exist. Copy op.json.example to "
              f"op.json and edit it before promoting.", file=sys.stderr)
        return 1

    live = load_live(live_path)
    live_entries = expand_live_to_entries(live)

    # Probe backends for live schemas. Each tool's inputSchema gets
    # canonicalised + hashed; the hash goes into the snapshot. At
    # runtime, `op({operation: "sync"})` compares this hash against
    # the live backend's current schema and reports any mismatches in
    # `changed_schemas`.
    probed_hashes: dict[tuple[str, str], str] = {}
    if not args.no_probe and live.backends:
        print(f"probing {len(live.backends)} backend(s) for live schemas...")
        try:
            probed = probe_backends_sync(
                list(live.backends), timeout_secs=args.probe_timeout,
            )
            probed_hashes = hash_lookup(probed)
            reachable = sum(1 for tools in probed.values() if tools)
            unreachable = [name for name, tools in probed.items() if not tools]
            print(f"  reached {reachable}/{len(live.backends)} backend(s)")
            if unreachable:
                print(f"  unreachable (no schema_hash will be written for "
                      f"these): {', '.join(unreachable)}")
        except Exception as e:
            print(f"  probe failed: {type(e).__name__}: {e}", file=sys.stderr)
            print(f"  proceeding without schema_hash. Re-run with backends "
                  f"reachable to enable schema-drift detection.",
                  file=sys.stderr)

    # Enrich live_entries with schema hashes from the probe. Domain ops
    # get the probed hash if available; meta-ops + unreachable ops keep
    # schema_hash=None.
    enriched_entries = tuple(
        SnapshotEntry(
            namespace=e.namespace,
            name=e.name,
            summary=e.summary,
            schema_hash=_hash_for_entry(e, probed_hashes),
        )
        for e in live_entries
    )

    # Carry highlights from existing snapshot if present + requested.
    highlights: tuple[str, ...] = ()
    prior: Snapshot | None = None
    if snap_path.exists():
        prior = load_snapshot(snap_path)
        if args.keep_highlights:
            highlights = prior.highlights

    # Drop highlights that no longer exist in the live registry. Better
    # to silently prune than ship a snapshot that names a missing op.
    valid_names = {op.name for op in enriched_entries}
    pruned_highlights = tuple(h for h in highlights if h in valid_names)
    dropped_highlights = [h for h in highlights if h not in valid_names]

    # Compute new version.
    new_version = args.version or _next_version(prior.snapshot_version if prior else None)
    new_hash = canonical_hash(pruned_highlights, enriched_entries)
    promoted_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    new_snapshot = Snapshot(
        snapshot_version=new_version,
        promoted_at=promoted_at,
        hash=new_hash,
        highlights=pruned_highlights,
        ops=enriched_entries,
    )

    # Print a human summary.
    if prior:
        if prior.hash == new_hash:
            print(f"snapshot content unchanged (hash={new_hash[:16]}...). "
                  f"Bumping version anyway: {prior.snapshot_version} -> {new_version}.")
        else:
            print(f"snapshot content changed.")
            print(f"  prior hash:  {prior.hash[:24]}...")
            print(f"  new hash:    {new_hash[:24]}...")
            print(f"  ops count:   {len(prior.ops)} -> {len(enriched_entries)}")
            print(f"  version:     {prior.snapshot_version} -> {new_version}")
    else:
        print(f"creating initial snapshot at {snap_path}.")
        print(f"  version:     {new_version}")
        print(f"  ops count:   {len(enriched_entries)}")
        print(f"  hash:        {new_hash[:24]}...")
    if probed_hashes:
        with_hash = sum(1 for e in enriched_entries if e.schema_hash)
        print(f"  schema_hash: {with_hash}/{len(enriched_entries)} ops")

    if dropped_highlights:
        print(f"  pruned {len(dropped_highlights)} highlights no longer in live registry: "
              f"{', '.join(dropped_highlights)}")

    if args.dry_run:
        print("dry-run: no file written.")
        return 0

    snap_path.write_text(
        json.dumps(new_snapshot.to_dict(), indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {snap_path}.")
    print()
    print("Restart any running Claude session to pick up the new snapshot.")
    print("The next session's first call will pay one prompt-cache miss; subsequent")
    print("calls within the session are warm. Future sessions stay warm too as long")
    print("as the snapshot doesn't change.")
    return 0


def _hash_for_entry(
    entry: SnapshotEntry,
    probed: dict[tuple[str, str], str],
) -> str | None:
    """Look up the probed schema_hash for one snapshot entry.

    Meta-ops have no backend schema, so they return None — they're
    described by the gateway itself. Domain ops translate
    `<namespace>.<tool_name>` into the probed map's lookup key.
    """
    if entry.namespace == "meta":
        return None
    if "." not in entry.name:
        return None
    _, _, tool_name = entry.name.partition(".")
    return probed.get((entry.namespace, tool_name))


def _next_version(prior: str | None) -> str:
    """Auto-increment the patch component of `prior` (semver-ish).

    `1.0.3` -> `1.0.4`. `1.2`   -> `1.2.1`. `0.0.1` (the example default)
    -> `0.0.2`. If `prior` is None, returns `0.0.1`.

    Doesn't try to be clever about pre-release tags or build metadata —
    if the user wants something specific they pass `--version`."""
    if prior is None:
        return "0.0.1"
    parts = prior.split(".")
    try:
        if len(parts) == 1:
            return f"{int(parts[0])}.0.1"
        if len(parts) == 2:
            return f"{parts[0]}.{parts[1]}.1"
        if len(parts) >= 3:
            head = parts[:-1]
            patch = int(parts[-1]) + 1
            return ".".join([*head, str(patch)])
    except ValueError:
        pass
    # Couldn't parse — append .1
    return f"{prior}.1"


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
