"""Dispatch CLI subcommands.

Subcommands:
  promote     Regenerate op.snapshot.json from op.json (eats one cache miss
              next session).
  diff        Show drift between op.snapshot.json and op.json without
              changing anything.
  validate    Check op.json for internal consistency (parses, no duplicate
              op names within a backend, etc.).
"""
from __future__ import annotations

import sys

from . import promote as _promote
from . import diff as _diff
from . import register as _register
from . import validate as _validate


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        _print_usage()
        return 2
    cmd, *rest = argv
    if cmd in ("-h", "--help", "help"):
        _print_usage()
        return 0
    if cmd == "promote":
        return _promote.run(rest)
    if cmd == "diff":
        return _diff.run(rest)
    if cmd == "validate":
        return _validate.run(rest)
    if cmd == "register":
        return _register.run(rest)
    print(f"unknown subcommand: {cmd!r}", file=sys.stderr)
    _print_usage()
    return 2


def _print_usage() -> None:
    print("usage: python -m op_cli <subcommand>")
    print()
    print("Subcommands:")
    print("  promote   Regenerate op.snapshot.json from op.json.")
    print("            Eats one Anthropic prompt-cache miss on the next session,")
    print("            then stable until the next promote.")
    print("  diff      Show drift between op.snapshot.json and op.json without")
    print("            changing anything.")
    print("  validate  Check op.json for internal consistency.")
    print("  register  Wire `op` into ~/.claude.json (or another MCP config).")


if __name__ == "__main__":
    raise SystemExit(main())
