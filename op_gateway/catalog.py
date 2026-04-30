"""Build the SDK-facing tool description from a snapshot.

The description is what Anthropic's prompt-cache hashes, so it must be
deterministic given the snapshot and stable across unrelated changes.

Highlights are interpolated as a small bullet list inside the
description; the full catalog is NOT — it lives in the live `list`
op, which means catalog changes don't churn the description's bytes
unless they also affect highlights or the meta-op set.
"""
from __future__ import annotations

from .manifest import Snapshot


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
    varies here invalidates the prompt cache."""
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
    lines.append(USAGE_DESCRIPTION.rstrip())
    return "\n".join(lines)
