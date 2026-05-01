"""Description-text builder. The bytes of the description are the SDK's
cache key, so this test pins the structure."""
from __future__ import annotations

from op_gateway.catalog import build_description
from op_gateway.manifest import Snapshot, SnapshotEntry


def _snap(highlights: tuple[str, ...]) -> Snapshot:
    return Snapshot(
        snapshot_version="0.0.1",
        promoted_at="2026-04-30T00:00:00Z",
        hash="sha256:placeholder",
        highlights=highlights,
        ops=(
            SnapshotEntry("meta", "list", "Enumerate available ops"),
            SnapshotEntry("recap", "recap.recap", "Catch up"),
        ),
    )


def test_description_mentions_meta_ops() -> None:
    desc = build_description(_snap(()))
    assert "list" in desc
    assert "describe" in desc
    assert "sync" in desc
    assert "health" in desc
    assert "manifest_version" in desc


def test_description_mentions_invocation_shape() -> None:
    desc = build_description(_snap(()))
    assert "operation" in desc
    assert "args" in desc


def test_description_includes_highlights_when_present() -> None:
    desc = build_description(_snap(("recap.recap", "chatfork.fork")))
    assert "recap.recap" in desc
    assert "chatfork.fork" in desc
    assert "HIGHLIGHTS" in desc


def test_description_omits_highlights_section_when_empty() -> None:
    desc = build_description(_snap(()))
    assert "HIGHLIGHTS" not in desc


def test_description_includes_full_catalog_grouped_by_namespace() -> None:
    """Every domain op is named in the description so a fresh chat
    knows what `op` can route to without first calling `list`."""
    snap = Snapshot(
        snapshot_version="0.0.1",
        promoted_at="2026-04-30T00:00:00Z",
        hash="sha256:placeholder",
        highlights=(),
        ops=(
            SnapshotEntry("widgets", "widgets.frobnicate", "Frob a widget"),
            SnapshotEntry("widgets", "widgets.calibrate",  "Calibrate a widget"),
            SnapshotEntry("recap",   "recap.recap",        "Catch up"),
        ),
    )
    desc = build_description(snap)
    assert "FULL CATALOG" in desc
    # Every op surfaces with its summary
    assert "widgets.frobnicate — Frob a widget" in desc
    assert "widgets.calibrate — Calibrate a widget" in desc
    assert "recap.recap — Catch up" in desc
    # Namespace headers render
    assert "  widgets:" in desc
    assert "  recap:" in desc


def test_description_omits_meta_ops_from_full_catalog() -> None:
    """Meta-ops are documented in META_OPS_DESCRIPTION; the full
    catalog only enumerates domain ops to avoid duplication."""
    snap = Snapshot(
        snapshot_version="0.0.1",
        promoted_at="2026-04-30T00:00:00Z",
        hash="sha256:placeholder",
        highlights=(),
        ops=(
            SnapshotEntry("meta",  "list",        "Enumerate"),
            SnapshotEntry("recap", "recap.recap", "Catch up"),
        ),
    )
    desc = build_description(snap)
    catalog_section = desc.split("FULL CATALOG")[1] if "FULL CATALOG" in desc else ""
    # The meta section above mentions `list`, but the FULL CATALOG block
    # itself shouldn't repeat it.
    assert "meta:" not in catalog_section


def test_description_orders_namespaces_and_ops_alphabetically() -> None:
    """Stable ordering is required so byte-equivalent snapshots produce
    byte-equivalent descriptions."""
    snap = Snapshot(
        snapshot_version="0.0.1",
        promoted_at="2026-04-30T00:00:00Z",
        hash="sha256:placeholder",
        highlights=(),
        ops=(
            SnapshotEntry("zebra", "zebra.b", "z-b"),
            SnapshotEntry("alpha", "alpha.b", "a-b"),
            SnapshotEntry("zebra", "zebra.a", "z-a"),
            SnapshotEntry("alpha", "alpha.a", "a-a"),
        ),
    )
    desc = build_description(snap)
    # alpha namespace before zebra
    assert desc.index("alpha:") < desc.index("zebra:")
    # alpha.a before alpha.b within alpha
    assert desc.index("alpha.a") < desc.index("alpha.b")
    # zebra.a before zebra.b within zebra
    assert desc.index("zebra.a") < desc.index("zebra.b")


def test_description_skips_catalog_section_when_only_meta_ops() -> None:
    """A snapshot with no domain ops (just meta-ops) should not emit an
    empty FULL CATALOG header."""
    snap = Snapshot(
        snapshot_version="0.0.1",
        promoted_at="2026-04-30T00:00:00Z",
        hash="sha256:placeholder",
        highlights=(),
        ops=(SnapshotEntry("meta", "list", "Enumerate"),),
    )
    desc = build_description(snap)
    assert "FULL CATALOG" not in desc


def test_description_changes_when_op_added_or_removed() -> None:
    """The full-catalog embed is the load-bearing property: adding,
    removing, or rewording an op MUST change the description bytes
    (and hence bust the prompt cache for new sessions). That's the
    explicit tradeoff — discovery beats byte-stability here."""
    desc_a = build_description(Snapshot(
        snapshot_version="0.0.1",
        promoted_at="2026-04-30T00:00:00Z",
        hash="sha256:a",
        highlights=(),
        ops=(SnapshotEntry("recap", "recap.recap", "x"),),
    ))
    desc_b = build_description(Snapshot(
        snapshot_version="0.0.1",
        promoted_at="2026-04-30T00:00:00Z",
        hash="sha256:b",
        highlights=(),
        ops=(
            SnapshotEntry("recap",   "recap.recap",     "x"),
            SnapshotEntry("recap",   "recap.recap_all", "y"),
            SnapshotEntry("chatfork","chatfork.fork",   "z"),
        ),
    ))
    assert desc_a != desc_b
    # And the new ops are surfaced in desc_b
    assert "recap.recap_all" in desc_b
    assert "chatfork.fork" in desc_b


def test_description_changes_when_highlights_change() -> None:
    desc_a = build_description(_snap(()))
    desc_b = build_description(_snap(("recap.recap",)))
    assert desc_a != desc_b
