# op

A single MCP server that proxies to every other MCP server you register on a machine.

The Claude Code SDK loads MCP servers in parallel at session start. When some
servers are slow, the SDK doesn't wait — it sends the first API call with a
partial tools array, then sends a `deferred_tools_delta` later. The first call
caches one tool list; the second call has a different tool list; Anthropic
returns `cache_miss_reason: tools_changed` and burns ~6% of the 5h Anthropic
allowance per fresh session start.

`op` collapses everything to ONE tool. From the SDK's perspective the tools
array contains `[built-ins…, op]`. One MCP server = one boot, deterministic
completion before first API call. No deferred-tools churn. The bytes never
change between sessions unless you explicitly choose to take a cache miss.

## Quickstart

```powershell
# Install at C:\D\op\
git clone https://github.com/joshcomley/op C:\D\op
cd C:\D\op
pip install -r requirements.txt

# Copy the example registry to your live registry
cp op.json.example op.json

# Promote to a fresh snapshot (regenerates op.snapshot.json from op.json)
python -m op_cli promote

# Register `op` in ~/.claude.json (or your project's .mcp.json)
# See INSTALL.md for the JSON snippet.
```

## How it works

Two files on disk, both at `C:\D\op\`:

| File | Role | Edit when |
|---|---|---|
| `op.json` | LIVE manifest — every backend MCP server you want to proxy | Any time, free |
| `op.snapshot.json` | FROZEN snapshot — what the SDK currently sees | Only via `op promote` |

The SDK caches the tool description built from `op.snapshot.json`. As long as
you don't run `op promote`, the cache stays warm forever — even if you edit
`op.json`, even if a backend MCP server crashes and reconnects, even if `mcpup`
pulls a new upstream tool at 3 am.

When you want the agent to learn about new tools mid-session, it calls
`op({operation: "sync"})` and gets a diff. Zero cache cost — the SDK's tool
description hasn't changed.

When you want the new tools to be permanent across sessions, you run
`op promote`. ONE cache miss next session, then stable forever.

## The `op` tool

The SDK sees one tool:

```
{
  name: "op",
  description: <stable, ~1.4k char summary>,
  parameters: {
    operation: string,    // "list" | "describe" | "sync" | "recap.recap" | ...
    args:      object?    // op-specific payload
  }
}
```

Calls look like:

```
op({operation: "list"})
op({operation: "describe", args: {operation: "recap.recap"}})
op({operation: "sync"})
op({operation: "health"})
op({operation: "recap.recap_all"})
op({operation: "chatfork.context_usage"})
```

## Meta-ops (always available)

| Op | Purpose |
|---|---|
| `list` | Enumerate every available op |
| `describe` | Full schema + docs for one op |
| `sync` | Diff between this session's snapshot and the live registry |
| `health` | Per-backend availability |
| `manifest_version` | Snapshot version + content hash for change detection |

## CLI

```powershell
op promote        # regenerate op.snapshot.json from op.json (eats one cache miss next session)
op diff           # show what would change without promoting
op validate       # verify every backend in op.json actually exposes the ops it claims
```

## Design

Full design document in
[`design.md`](design.md).
The original design discussion lives in cmd's
[`docs/ai/design-op-gateway.md`](https://github.com/joshcomley/cmd/blob/main/docs/ai/design-op-gateway.md).

## Status

**Phase 3 (current)**: schema-drift detection in `sync`. `op promote`
now probes each backend's live `tools/list` and writes a canonical
`schema_hash` per op into `op.snapshot.json`. At runtime,
`op({operation: "sync"})` compares the snapshot's hash against the
backend's current schema and reports any mismatches under
`changed_schemas`. The agent can then call `describe` to see the new
shape — all without invalidating the SDK's cached tool description.

**Phase 2**: backend wiring. Each backend in `op.json` runs as a
persistent stdio MCP child process; `tools/call` forwards over a
supervisor-managed connection that reconnects with exponential backoff
when a backend crashes. `health` reports real per-backend status;
`describe` returns real `inputSchema` from cached `tools/list`.

**Phase 1**: standalone gateway with meta-ops only. Still reachable via
`OP_DISABLE_POOL=1` or an empty `backends` array.

**Phase 4+**: full machine migration (port every MCP server in
`~/.claude.json` into `op.json`), hot-reload of `op.json` without
gateway restart, telemetry. Tracked as issues against this repo.
