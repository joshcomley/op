"""Build the SDK-facing tool description from a snapshot.

The description is what Anthropic's prompt-cache hashes, so it must be
deterministic given the snapshot.

Embeds the FULL catalog of proxied ops (grouped by namespace, with
summaries) so a fresh chat reads the start-of-session prompt and
already knows what `op` can route to — no `op({operation: "list"})`
discovery call needed before the first invocation. Highlights are
still emitted above the catalog for the curated frequently-used
subset.

Tradeoff: every snapshot promotion that adds, removes, or rewords an
op now changes the description bytes and busts the prompt cache for
new sessions. That's an explicit choice — a fresh agent that doesn't
know `chatfork.ctx` exists is a worse failure than a one-time cache
miss when ops are added. Adopters who want byte-stability can pin
`highlights` and skip promotions.
"""
from __future__ import annotations

from .manifest import Snapshot, SnapshotEntry


META_OPS_DESCRIPTION = """\
META-OPS (always available, unprefixed):
  list                  -> array of {name, summary, namespace} for every op.
                          Optional args: {namespace: "<prefix>"} to filter.
  describe              -> full JSON schema + docs for one op.
                          Required args: {operation: "<name>"}.
  sync                  -> diff between this session's snapshot and the live
                          registry. Returns {added, removed, changed_schemas}.
                          Use to discover ops added since this session started,
                          without invalidating any cache.
  health                -> per-backend status: up, down, reconnecting.
  manifest_version      -> snapshot version + content hash."""


USAGE_DESCRIPTION = """\
USAGE:
  - If you know the op name, call directly: op({operation: "recap.recap"}).
  - Discover the full catalog with op({operation: "list"}).
  - Get a schema before calling with op({operation: "describe",
    args: {operation: "<name>"}}).
  - On invalid args, the tool returns an error including the expected schema.
  - To learn about ops added since this session started:
    op({operation: "sync"}).
"""


def build_description(snapshot: Snapshot) -> str:
    """Build the static tool description text for the SDK.

    The bytes of the returned string ARE the cache key. Anything that
    varies here invalidates the prompt cache — including the full
    catalog block, which is intentional (see module docstring)."""
    lines: list[str] = []
    lines.append("Single dispatch tool for all gateway-routed operations.")
    lines.append("")
    lines.append("Invoke: {operation: \"<name>\", args?: {...}}")
    lines.append("")
    lines.append(META_OPS_DESCRIPTION)
    lines.append("")
    if snapshot.highlights:
        lines.append("HIGHLIGHTS (this snapshot's curated frequently-used ops):")
        for h in snapshot.highlights:
            lines.append(f"  {h}")
        lines.append("")
    catalog_block = _format_full_catalog(snapshot.ops)
    if catalog_block:
        lines.append(catalog_block)
        lines.append("")
    lines.append(USAGE_DESCRIPTION.rstrip())
    return "\n".join(lines)


def _format_full_catalog(ops: tuple[SnapshotEntry, ...]) -> str:
    """Render every domain op grouped by namespace, alphabetical within
    each. Meta-ops are omitted — they're already documented in
    META_OPS_DESCRIPTION and would duplicate that surface.

    Returns "" when there are no domain ops (the section is then
    skipped entirely)."""
    domain = [op for op in ops if op.namespace != "meta"]
    if not domain:
        return ""
    by_ns: dict[str, list[SnapshotEntry]] = {}
    for op in domain:
        by_ns.setdefault(op.namespace, []).append(op)
    lines: list[str] = [
        "FULL CATALOG (every proxied op this snapshot exposes, grouped by namespace):",
    ]
    for ns in sorted(by_ns.keys()):
        lines.append(f"  {ns}:")
        for op in sorted(by_ns[ns], key=lambda o: o.name):
            summary = op.summary.strip() or "(no summary)"
            lines.append(f"    {op.name} — {summary}")
    return "\n".join(lines)
