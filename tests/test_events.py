"""Telemetry event sink — JSONL append, env-gated, no-op default."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from op_gateway import events
from op_gateway.events import (
    EventSink,
    _NoopSink,
    current_sink,
    emit_backend_state,
    emit_dispatch,
    emit_reconcile,
    set_sink,
    sink_from_env,
)


@pytest.fixture(autouse=True)
def reset_sink():
    """Each test gets a fresh sink. Otherwise events from earlier tests
    accumulate in the module-level singleton."""
    previous = current_sink()
    yield
    set_sink(previous)


def test_default_sink_is_noop() -> None:
    """Module starts with no sink wired — emit() is a hot-path no-op."""
    set_sink(None)  # explicit
    assert isinstance(current_sink(), _NoopSink)
    # Should not raise even though no destination is configured.
    emit_dispatch(operation="x", duration_ms=1, is_meta=True, namespace=None, success=True)


def test_sink_from_env_returns_noop_without_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OP_EVENTS_FILE", raising=False)
    sink = sink_from_env()
    assert isinstance(sink, _NoopSink)


def test_sink_from_env_returns_real_sink_when_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "events.jsonl"
    monkeypatch.setenv("OP_EVENTS_FILE", str(target))
    sink = sink_from_env()
    assert isinstance(sink, EventSink)
    assert sink.path == target


def test_event_sink_writes_jsonl(tmp_path: Path) -> None:
    """Each emit() appends one valid JSON line. Multiple emits stack
    in append order. Each line has the canonical {ts, kind, data} shape."""
    target = tmp_path / "events.jsonl"
    sink = EventSink(target)
    set_sink(sink)
    try:
        emit_dispatch(
            operation="list", duration_ms=5, is_meta=True,
            namespace=None, success=True,
        )
        emit_dispatch(
            operation="recap.recap", duration_ms=120, is_meta=False,
            namespace="recap", success=True,
        )
    finally:
        sink.close()

    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    for line in lines:
        evt = json.loads(line)
        assert "ts" in evt
        assert evt["kind"] == "dispatch"
        assert "data" in evt

    first  = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["data"]["operation"] == "list"
    assert first["data"]["is_meta"] is True
    assert second["data"]["operation"] == "recap.recap"
    assert second["data"]["namespace"] == "recap"


def test_emit_backend_state_carries_diagnostic_fields(tmp_path: Path) -> None:
    """Backend state events include the transition + last_error +
    reconnect_attempt when relevant."""
    target = tmp_path / "events.jsonl"
    sink = EventSink(target)
    set_sink(sink)
    try:
        emit_backend_state(
            backend="recap", old_status="connecting", new_status="up",
        )
        emit_backend_state(
            backend="recap", old_status="up", new_status="reconnecting",
            last_error="stdio EOF", reconnect_attempt=2,
        )
    finally:
        sink.close()

    lines = target.read_text(encoding="utf-8").splitlines()
    one = json.loads(lines[0])
    two = json.loads(lines[1])

    assert one["kind"] == "backend_state"
    assert one["data"]["backend"] == "recap"
    assert one["data"]["new_status"] == "up"
    # No last_error / reconnect_attempt -> not in payload (keeps lines small)
    assert "last_error" not in one["data"]

    assert two["data"]["new_status"] == "reconnecting"
    assert two["data"]["last_error"] == "stdio EOF"
    assert two["data"]["reconnect_attempt"] == 2


def test_emit_reconcile_groups_actions(tmp_path: Path) -> None:
    """Reconcile event maps each backend to its action and also
    surfaces convenience lists per category."""
    target = tmp_path / "events.jsonl"
    sink = EventSink(target)
    set_sink(sink)
    try:
        emit_reconcile({
            "alpha":   "unchanged",
            "beta":    "started",
            "gamma":   "stopped",
            "delta":   "restarted",
        })
    finally:
        sink.close()

    line = target.read_text(encoding="utf-8").strip()
    evt = json.loads(line)
    assert evt["kind"] == "reconcile"
    assert evt["data"]["actions"] == {
        "alpha":   "unchanged",
        "beta":    "started",
        "gamma":   "stopped",
        "delta":   "restarted",
    }
    assert evt["data"]["started"]   == ["beta"]
    assert evt["data"]["stopped"]   == ["gamma"]
    assert evt["data"]["restarted"] == ["delta"]
    assert evt["data"]["unchanged"] == ["alpha"]


def test_event_sink_io_failure_silently_drops(tmp_path: Path) -> None:
    """If the events file becomes unwritable mid-run, the sink marks
    itself closed and subsequent emits are silent. The gateway keeps
    running."""
    target = tmp_path / "events.jsonl"
    sink = EventSink(target)
    set_sink(sink)
    try:
        emit_dispatch(operation="a", duration_ms=1, is_meta=True,
                      namespace=None, success=True)
        # Simulate a write failure by closing the underlying file under
        # the sink's feet. Subsequent emits should silently drop rather
        # than raise.
        if sink._file is not None:
            sink._file.close()
            sink._file = None
            sink._closed = True

        # This should NOT raise.
        emit_dispatch(operation="b", duration_ms=1, is_meta=True,
                      namespace=None, success=True)
    finally:
        sink.close()


def test_event_sink_close_is_idempotent(tmp_path: Path) -> None:
    sink = EventSink(tmp_path / "events.jsonl")
    sink.close()
    sink.close()  # second close shouldn't throw


# --- end-to-end integration via the dispatcher ---------------------------

async def test_dispatch_emits_event_per_call(
    tmp_path: Path,
) -> None:
    """A successful dispatch emits one event with operation + namespace
    + success=True. Used by cmd's /workers page (eventually) + ad-hoc
    cache-burn analysis."""
    from op_gateway import dispatch
    from op_gateway.manifest import LiveManifest, Snapshot, SnapshotEntry

    target = tmp_path / "events.jsonl"
    sink = EventSink(target)
    set_sink(sink)
    try:
        snap = Snapshot(
            snapshot_version="0.0.1",
            promoted_at="2026-04-30T00:00:00Z",
            hash="sha256:placeholder",
            highlights=(),
            ops=(SnapshotEntry("meta", "list", "..."),),
        )
        live = LiveManifest("1", {}, ())
        await dispatch.dispatch("list", None, snap, live, None)
    finally:
        sink.close()

    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    evt = json.loads(lines[0])
    assert evt["kind"] == "dispatch"
    assert evt["data"]["operation"] == "list"
    assert evt["data"]["is_meta"] is True
    assert evt["data"]["namespace"] is None
    assert evt["data"]["success"] is True


async def test_dispatch_logs_error_on_failure(tmp_path: Path) -> None:
    """A dispatch that returns an `error` dict is logged with success=False
    + the error message."""
    from op_gateway import dispatch
    from op_gateway.manifest import LiveManifest, Snapshot

    target = tmp_path / "events.jsonl"
    sink = EventSink(target)
    set_sink(sink)
    try:
        snap = Snapshot(
            snapshot_version="0.0.1",
            promoted_at="2026-04-30T00:00:00Z",
            hash="sha256:placeholder",
            highlights=(),
            ops=(),
        )
        live = LiveManifest("1", {}, ())
        await dispatch.dispatch("ghost.nope", None, snap, live, None)
    finally:
        sink.close()

    lines = target.read_text(encoding="utf-8").splitlines()
    evt = json.loads(lines[0])
    assert evt["data"]["success"] is False
    assert "error" in evt["data"]
    assert "namespace" in evt["data"]["error"].lower()
