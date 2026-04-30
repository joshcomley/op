# Installing `op`

## 1. Clone + dependencies

```powershell
git clone https://github.com/joshcomley/op C:\D\op
cd C:\D\op
.\install.ps1
```

`install.ps1` runs `pip install`, seeds `op.json` from the example if you
don't already have one, and runs `op promote` to generate the initial
`op.snapshot.json`.

## 2. Register `op` in your MCP config

`op` is a stdio MCP server. Register it in `~/.claude.json` (machine-wide)
or in a project's `.mcp.json` (project-only).

Add this entry under `"mcpServers"`:

```json
"op": {
  "command": "python",
  "args":    ["-m", "op_gateway.server"],
  "cwd":     "C:\\D\\op"
}
```

Restart any running Claude session to pick it up. The agent will see one
tool named `op` with the meta-ops + your op.json's backends as the catalog.

## 3. Verify

In a Claude session, ask the agent to call `op`:

```
op({operation: "list"})
op({operation: "manifest_version"})
op({operation: "health"})
```

`list` should return the meta-ops plus any domain ops from `op.json`'s
backends. `manifest_version` returns the snapshot's version + content
hash. `health` reports per-backend status (Phase 1: every backend
shows `not_connected` because backend pool wiring is Phase 2).

## 4. Day-to-day workflow

Edit `op.json` whenever you want to add/remove/change backends. **No
cache cost** — the SDK still sees the snapshot.

Mid-session, ask the agent to call `op({operation: "sync"})` to discover
ops added since the snapshot was promoted. Still no cache cost — the
agent learns via tool result, the cached description doesn't change.

When you're ready for the new ops to be visible across sessions
permanently, run:

```powershell
cd C:\D\op
python -m op_cli promote
```

This regenerates `op.snapshot.json` from `op.json`. **One Anthropic
prompt-cache miss on the next session** (≈ 6% of your 5h allowance for a
700k-token session — paid once), then stable until the next promote.

## Migrating other MCP servers into `op`

Right now `~/.claude.json` likely has many MCP servers registered
(chatfork-mcp, recap-mcp, biosphere-workspace-mcp, etc.). Each one
contributes to the SDK's tools array, and the slow ones cause the
`tools_changed` cache miss class.

Migration is incremental and reversible:

1. Pick one MCP server. Add it to `op.json` as a backend.
2. Run `python -m op_cli promote`. Restart your Claude session.
3. Verify the agent can invoke the backend's tools via
   `op({operation: "<backend>.<tool>"})`.
4. **Remove** that server's standalone entry from `~/.claude.json`.
5. Repeat for the next server.

After migrating everything, `~/.claude.json` registers exactly one MCP
server: `op`.
