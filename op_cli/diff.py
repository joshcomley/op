"""`op diff` — show drift between op.snapshot.json and op.json without
changing anything. Mirrors what `op({operation: "sync"})` returns to
the agent at runtime, but printed for the human."""
from __future__ import annotations

import argparse
import json
import sys

from op_gateway import paths
from op_gateway.diff import diff
from op_gateway.manifest import load_live, load_snapshot


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="op diff")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON (the same shape `op({operation: \"sync\"})` returns). "
             "Default is human-readable text.",
    )
    args = parser.parse_args(argv)

    live_path = paths.live_manifest_path()
    snap_path = paths.snapshot_path()

    if not live_path.exists():
        print(f"error: {live_path} does not exist.", file=sys.stderr)
        return 1
    if not snap_path.exists():
        print(f"error: {snap_path} does not exist. Run `op promote` first.",
              file=sys.stderr)
        return 1

    live = load_live(live_path)
    snapshot = load_snapshot(snap_path)
    result = diff(snapshot, live)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
        return 0

    print(f"snapshot version:   {result.snapshot_version}")
    print(f"snapshot hash:      {result.snapshot_hash[:24]}...")
    print(f"live state hash:    {result.live_state_hash[:24]}...")
    print(f"drifted:            {result.is_drifted}")
    print()
    if not result.is_drifted:
        print("snapshot is in sync with live registry.")
        return 0
    if result.added:
        print(f"ADDED ({len(result.added)}):")
        for op in result.added:
            print(f"  + {op['name']:<48} {op.get('summary', '')}")
        print()
    if result.removed:
        print(f"REMOVED ({len(result.removed)}):")
        for op in result.removed:
            print(f"  - {op['name']:<48}")
        print()
    print("Run `op promote` to update the snapshot. One cache miss next session,")
    print("then stable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
