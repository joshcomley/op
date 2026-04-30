"""`op register` — wire `op` into ~/.claude.json (or another MCP config).

Idempotent: re-running updates the entry in place. Backs up the
target file before writing so any malformed result can be rolled back.

Usage:
  python -m op_cli register                    # default ~/.claude.json
  python -m op_cli register --target=path      # explicit target
  python -m op_cli register --remove           # remove the op entry
  python -m op_cli register --print-only       # show the JSON; don't write
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

from op_gateway import paths


_DEFAULT_TARGET = Path.home() / ".claude.json"
_ENTRY_KEY = "op"


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="op register")
    parser.add_argument(
        "--target",
        type=Path,
        default=_DEFAULT_TARGET,
        help=f"MCP config file to update. Default: {_DEFAULT_TARGET}",
    )
    parser.add_argument(
        "--cwd",
        type=Path,
        default=paths.op_home(),
        help="cwd for the op-gateway subprocess. Default: op's install root.",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python interpreter to invoke op-gateway with. "
             f"Default: {sys.executable}",
    )
    parser.add_argument(
        "--remove",
        action="store_true",
        help="Remove the `op` entry from the target's mcpServers map.",
    )
    parser.add_argument(
        "--print-only",
        action="store_true",
        help="Print the JSON entry that WOULD be written. Don't touch the file.",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip the .bak.<ts> backup of the target file before writing.",
    )
    args = parser.parse_args(argv)

    op_entry = {
        "command": str(args.python),
        "args":    ["-m", "op_gateway.server"],
        "cwd":     str(args.cwd),
    }

    if args.print_only:
        print(json.dumps({_ENTRY_KEY: op_entry}, indent=2))
        return 0

    target: Path = args.target

    if not target.exists():
        if args.remove:
            print(f"target {target} doesn't exist; nothing to remove.")
            return 0
        print(f"target {target} doesn't exist; creating with empty mcpServers.")
        config: dict = {"mcpServers": {}}
    else:
        try:
            config = json.loads(target.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"error: failed to parse {target}: {e}", file=sys.stderr)
            return 1

    if not isinstance(config, dict):
        print(f"error: {target} root must be a JSON object", file=sys.stderr)
        return 1

    servers = config.get("mcpServers")
    if servers is None:
        servers = {}
        config["mcpServers"] = servers
    elif not isinstance(servers, dict):
        print(f"error: {target}.mcpServers must be a JSON object", file=sys.stderr)
        return 1

    if args.remove:
        if _ENTRY_KEY not in servers:
            print(f"`op` not registered in {target}; nothing to do.")
            return 0
        existing = servers.pop(_ENTRY_KEY)
        print(f"removed `op` entry from {target}.")
        print(f"  was: {json.dumps(existing)}")
    else:
        existing = servers.get(_ENTRY_KEY)
        if existing == op_entry:
            print(f"`op` already registered in {target} with these settings; "
                  f"no change.")
            return 0
        servers[_ENTRY_KEY] = op_entry
        if existing is None:
            print(f"adding `op` entry to {target}.")
        else:
            print(f"updating `op` entry in {target}.")
            print(f"  was: {json.dumps(existing)}")
        print(f"  now: {json.dumps(op_entry)}")

    if not args.no_backup and target.exists():
        backup = target.with_suffix(target.suffix + f".bak.{int(time.time())}")
        shutil.copy2(target, backup)
        print(f"  backed up to {backup}")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    print()
    if args.remove:
        print("Restart any running Claude session to pick up the removal.")
    else:
        print("Restart any running Claude session to pick up the new MCP server.")
        print("Then in any session, the agent can call:")
        print("  op({operation: \"list\"})           - see the catalog")
        print("  op({operation: \"manifest_version\"})  - snapshot version + hash")
        print("  op({operation: \"<namespace>.<tool>\"}) - dispatch to a backend")
    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
