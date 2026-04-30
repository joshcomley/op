"""Structured event sink — append JSONL lines to a configured file.

Off by default (no env var = no telemetry). Enable via the
`OP_EVENTS_FILE` env var; the file is opened append-only on first
event, kept open for the gateway's lifetime, flushed after every
write so a crash doesn't lose recent events.

Three event categories cover the gateway's interesting state:

  dispatch       — every op() call. operation, args size, duration,
                   success/failure, backend (for domain ops).
  backend_state  — backend status transitions (connecting -> up,
                   up -> reconnecting, etc.).
  reconcile      — hot-reload reconciliation actions.

The sink is best-effort: I/O failures are logged + swallowed so a
broken telemetry file doesn't break the gateway. Closing the sink
flushes one final time and closes the underlying file.

Format is JSONL (one event per line) so downstream tools can stream-
parse without loading the whole file. Each event has a `ts`
(ISO 8601 UTC), `kind` (one of the three above), and a `data` block
whose shape depends on `kind`.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


log = logging.getLogger(__name__)


_EVENTS_FILE_ENV = "OP_EVENTS_FILE"


class EventSink:
    """Append-only JSONL writer. Thread-safe (uses a lock around
    write+flush) so background tasks can call from multiple coroutines /
    threads without interleaving."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._file: Any = None
        self._closed = False

    def _ensure_open(self) -> None:
        if self._file is not None or self._closed:
            return
        try:
            # Make sure the parent dir exists. mkdir is idempotent so
            # it's safe under races.
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._file = open(self.path, "a", encoding="utf-8", buffering=1)
        except OSError as e:
            log.warning(
                "op-events: cannot open %s for append (%s); telemetry "
                "will be silently dropped until next sink rotation.",
                self.path, e,
            )
            self._closed = True

    def emit(self, kind: str, data: dict[str, Any]) -> None:
        """Append one event. Best-effort: any I/O error is logged and
        swallowed so a broken sink doesn't take down the gateway."""
        if self._closed:
            return
        event = {
            "ts":   datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            "kind": kind,
            "data": data,
        }
        line = json.dumps(event, separators=(",", ":")) + "\n"
        with self._lock:
            self._ensure_open()
            if self._file is None:
                return
            try:
                self._file.write(line)
                self._file.flush()
            except OSError as e:
                log.warning("op-events: write failed (%s); marking sink closed.", e)
                try:
                    self._file.close()
                except Exception:
                    pass
                self._file = None
                self._closed = True

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._file is not None:
                try:
                    self._file.flush()
                    self._file.close()
                except OSError:
                    pass
                self._file = None


class _NoopSink:
    """No-telemetry default. emit() is a hot-path no-op."""
    def emit(self, kind: str, data: dict[str, Any]) -> None:
        return
    def close(self) -> None:
        return


def sink_from_env() -> Any:
    """Build an EventSink if OP_EVENTS_FILE is set, otherwise return a
    no-op sink. Called once at gateway startup."""
    target = os.environ.get(_EVENTS_FILE_ENV)
    if not target:
        return _NoopSink()
    return EventSink(Path(target))


# Module-level singleton wired by the FastMCP lifespan. Tools that
# want to emit events go through `current_sink()` so they pick up the
# live sink (or the noop fallback when no lifespan is running).
_sink: Any = _NoopSink()


def set_sink(sink: Any) -> None:
    """Wire the live sink during lifespan setup. Pass _NoopSink() (or
    None, for the same effect) on tear-down."""
    global _sink
    _sink = sink if sink is not None else _NoopSink()


def current_sink() -> Any:
    return _sink


# ---------------------------------------------------------------------
# Domain-specific helpers — one per event kind. Keep the shape
# centralised here so consumers (cmd /workers, ad-hoc analysis) get
# stable field names.
# ---------------------------------------------------------------------

def emit_dispatch(
    *,
    operation: str,
    duration_ms: int,
    is_meta: bool,
    namespace: str | None,
    success: bool,
    error: str | None = None,
) -> None:
    """Emit one dispatch event. `args` aren't logged — they may
    contain user content."""
    current_sink().emit("dispatch", {
        "operation":   operation,
        "duration_ms": duration_ms,
        "is_meta":     is_meta,
        "namespace":   namespace,
        "success":     success,
        **({"error": error} if error else {}),
    })


def emit_backend_state(
    *,
    backend: str,
    old_status: str,
    new_status: str,
    last_error: str | None = None,
    reconnect_attempt: int = 0,
) -> None:
    """Backend state transition (up <-> reconnecting <-> down etc.)."""
    current_sink().emit("backend_state", {
        "backend":           backend,
        "old_status":        old_status,
        "new_status":        new_status,
        **({"last_error": last_error} if last_error else {}),
        **({"reconnect_attempt": reconnect_attempt} if reconnect_attempt else {}),
    })


def emit_reconcile(actions: dict[str, str]) -> None:
    """Hot-reload reconcile result. `actions` maps backend name to
    one of {"started", "stopped", "restarted", "unchanged"}."""
    current_sink().emit("reconcile", {
        "actions": actions,
        "started":    [n for n, a in actions.items() if a == "started"],
        "stopped":    [n for n, a in actions.items() if a == "stopped"],
        "restarted":  [n for n, a in actions.items() if a == "restarted"],
        "unchanged":  [n for n, a in actions.items() if a == "unchanged"],
    })
