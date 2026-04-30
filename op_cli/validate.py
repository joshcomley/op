"""`op validate` — check op.json is internally consistent.

Phase 1 only does syntactic + structural checks (no live backend
spawning). Phase 2 will add `validate --live` to compare declared ops
against each backend's `tools/list` output.

Exit codes:
  0  op.json is valid
  1  op.json is missing or malformed
  2  op.json has structural issues (duplicate names, etc.)
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter

from op_gateway import paths
from op_gateway.manifest import load_live


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="op validate")
    parser.parse_args(argv)

    live_path = paths.live_manifest_path()
    if not live_path.exists():
        print(f"error: {live_path} does not exist.", file=sys.stderr)
        return 1

    try:
        live = load_live(live_path)
    except Exception as e:
        print(f"error: failed to parse {live_path}: {e}", file=sys.stderr)
        return 1

    errors: list[str] = []

    # Backend-name uniqueness.
    backend_names = [b.name for b in live.backends]
    dups = [name for name, count in Counter(backend_names).items() if count > 1]
    if dups:
        errors.append(f"duplicate backend names: {sorted(dups)}")

    # Per-backend op-name uniqueness.
    for backend in live.backends:
        op_names = [op.name for op in backend.ops]
        op_dups = [n for n, c in Counter(op_names).items() if c > 1]
        if op_dups:
            errors.append(
                f"backend {backend.name!r}: duplicate op names: {sorted(op_dups)}"
            )

    # Empty / missing required fields.
    for backend in live.backends:
        if not backend.command:
            errors.append(f"backend {backend.name!r}: empty command")
        if not backend.ops:
            errors.append(f"backend {backend.name!r}: no ops declared "
                          "(backend would be invisible to the agent)")

    # Cross-namespace name collisions don't matter — namespacing is
    # explicit (recap.foo vs chatfork.foo). But name collisions WITHIN
    # a backend would be a routing ambiguity. Already covered above.

    if errors:
        print(f"{len(errors)} issue(s) found in {live_path}:")
        for e in errors:
            print(f"  - {e}")
        return 2

    print(f"op.json valid. {len(live.backends)} backend(s), "
          f"{sum(len(b.ops) for b in live.backends)} op(s) total.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
