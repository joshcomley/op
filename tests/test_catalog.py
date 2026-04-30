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


def test_description_does_not_include_full_catalog() -> None:
    """The catalog itself must NOT be in the description — that's what
    `list` is for. Embedding it would mean every op addition busts the
    cache, defeating the whole point.

    NOTE: the USAGE block of the description contains ONE hardcoded example
    invocation `op({operation: "recap.recap"})` for illustrative purposes.
    That's intentionally static — it's the same string regardless of what
    the snapshot's catalog contains. So we check for a different
    op name that the static template doesn't reference.
    """
    snap = Snapshot(
        snapshot_version="0.0.1",
        promoted_at="2026-04-30T00:00:00Z",
        hash="sha256:placeholder",
        highlights=(),
        ops=(
            SnapshotEntry("widgets", "widgets.frobnicate", "..."),
            SnapshotEntry("widgets", "widgets.calibrate",  "..."),
        ),
    )
    desc = build_description(snap)
    assert "widgets.frobnicate" not in desc
    assert "widgets.calibrate" not in desc


def test_description_byte_stable_across_op_additions() -> None:
    """Adding an op to the catalog WITHOUT adding it to highlights MUST
    NOT change the description bytes. This is the load-bearing property."""
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
    assert desc_a == desc_b


def test_description_changes_when_highlights_change() -> None:
    desc_a = build_description(_snap(()))
    desc_b = build_description(_snap(("recap.recap",)))
    assert desc_a != desc_b
