"""Backend MCP server connection pool.

Each backend in `op.json` becomes a long-running child process driven via
the standard MCP stdio protocol (the same protocol Claude Code uses to
talk to `op` itself). The pool spawns every backend at gateway startup,
keeps the connections open for the gateway's lifetime, and forwards
`tools/call` requests as they arrive.

State machine per backend:

  not_started --start_all()--> connecting --(initialize ok)--> up
                                  ^                            |
                                  |                            v
                                  +-------- (crash / EOF) ---  reconnecting
                                  |
                                  +-------- (start failed) --- down

When a connection drops, a background task waits with backoff and tries
again. Until it succeeds, ops routed to that backend return a structured
`tool_use_error` so the agent can self-correct (or just call `op({operation:
"sync"})` to discover the unavailability).

The pool itself is owned by the FastMCP server's lifespan: it starts when
the gateway process spawns and stops when the SDK closes the stdio. There's
exactly one pool per gateway process; concurrent calls are serialised
per-backend by `asyncio.Lock` so two `tools/call` requests don't interleave
on the same stdio.
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .manifest import BackendDef


log = logging.getLogger(__name__)


# Reconnect backoff (seconds). Caps at 60s so a permanently-failing
# backend doesn't burn CPU but a transiently-failing one recovers fast.
_RECONNECT_DELAYS = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0, 60.0)

# Default per-call timeout. Backends shouldn't take longer than this on
# any single tool call; if they do, something's deadlocked and killing
# the connection is the recovery.
DEFAULT_CALL_TIMEOUT_SECS = 60.0


# Status string constants. Centralised so the meta-op `health` and
# tests use the same vocabulary.
STATUS_NOT_STARTED  = "not_started"
STATUS_CONNECTING   = "connecting"
STATUS_UP           = "up"
STATUS_DOWN         = "down"
STATUS_RECONNECTING = "reconnecting"
STATUS_STOPPED      = "stopped"


class BackendUnavailable(Exception):
    """Raised when a domain op is routed to a backend that isn't `up`.

    The dispatch layer turns this into a structured `tool_use_error` so
    the agent sees the reason without an internal stack trace."""


@dataclass
class BackendStatus:
    """Snapshot of one backend's health + last-known state."""
    name: str
    status: str
    started_at: float | None = None
    last_seen: float | None = None
    last_error: str | None = None
    next_retry_at: float | None = None
    reconnect_attempt: int = 0
    tool_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name":   self.name,
            "status": self.status,
        }
        if self.started_at is not None:
            d["uptime_secs"] = round(time.time() - self.started_at, 1)
        if self.last_seen is not None:
            d["last_seen_secs_ago"] = round(time.time() - self.last_seen, 1)
        if self.last_error:
            d["last_error"] = self.last_error
        if self.next_retry_at is not None:
            d["next_retry_in_secs"] = max(0.0, round(self.next_retry_at - time.time(), 1))
        if self.reconnect_attempt:
            d["reconnect_attempt"] = self.reconnect_attempt
        if self.tool_count:
            d["tool_count"] = self.tool_count
        return d


class BackendConnection:
    """One persistent MCP-stdio connection to one backend.

    Holds the async context for the spawned subprocess + the ClientSession
    that talks MCP over its stdio. Caches the backend's `tools/list` result
    so `describe` and routing checks don't pay a round-trip per call.
    """

    def __init__(self, defn: BackendDef) -> None:
        self.defn = defn
        self.status = BackendStatus(name=defn.name, status=STATUS_NOT_STARTED)
        self._stack:    AsyncExitStack | None    = None
        self._session:  ClientSession | None     = None
        # Cached tools/list result. Populated on every successful initialize().
        # Live data (real schemas + descriptions); doesn't mutate during a
        # connection's life.
        self._tools:    list[Any] = []
        # Per-backend lock so two simultaneous tool calls don't interleave
        # on the same stdio (which would corrupt the JSON-RPC framing).
        self._call_lock: asyncio.Lock = asyncio.Lock()
        # The supervisor task that drives connect / wait / reconnect.
        # None until start() is called.
        self._supervisor: asyncio.Task[None] | None = None
        # Closed flag so the supervisor knows when to exit cleanly.
        self._closing: bool = False

    # ----- public ---------------------------------------------------

    async def start(self) -> None:
        """Spin up the supervisor task. Doesn't wait for the first
        connection — caller can poll status or call `wait_until_up`."""
        if self._supervisor and not self._supervisor.done():
            return
        self._closing = False
        self._supervisor = asyncio.create_task(
            self._run(), name=f"backend-supervisor-{self.defn.name}",
        )

    async def stop(self) -> None:
        """Tell the supervisor to exit and wait for it.

        Idempotent. The supervisor cleans up its own context stack; we
        just signal closure and await."""
        self._closing = True
        if self._supervisor and not self._supervisor.done():
            self._supervisor.cancel()
            try:
                await self._supervisor
            except (asyncio.CancelledError, Exception):
                pass
        self._supervisor = None
        self.status.status = STATUS_STOPPED

    async def wait_until_up(self, timeout_secs: float = 5.0) -> bool:
        """Block until the backend reaches `up` or the timeout expires.

        Useful in tests + on first call after a fresh start. Returns
        True if up, False if timed out."""
        deadline = time.monotonic() + timeout_secs
        while self.status.status != STATUS_UP:
            if time.monotonic() >= deadline:
                return False
            if self._closing:
                return False
            await asyncio.sleep(0.05)
        return True

    @property
    def tools(self) -> list[Any]:
        """The backend's last-known tool catalog. Returns the live MCP
        Tool objects from the SDK so callers can inspect schemas, etc."""
        return list(self._tools)

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
        *,
        timeout_secs: float = DEFAULT_CALL_TIMEOUT_SECS,
    ) -> dict[str, Any]:
        """Forward a `tools/call` to the backend over its stdio session.

        Raises BackendUnavailable if the backend isn't `up`. Raises
        asyncio.TimeoutError on hang. The returned dict shape matches
        what FastMCP wants the parent op call to return — content blocks
        + isError flag.
        """
        if self.status.status != STATUS_UP or self._session is None:
            raise BackendUnavailable(
                f"backend {self.defn.name!r} is {self.status.status!r}"
                + (f" (last_error: {self.status.last_error})" if self.status.last_error else "")
            )
        async with self._call_lock:
            try:
                result = await asyncio.wait_for(
                    self._session.call_tool(tool_name, arguments=arguments or {}),
                    timeout=timeout_secs,
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                # Timing out a call almost certainly means the backend is
                # stuck. Tear down so the supervisor reconnects.
                self.status.last_error = f"call_tool({tool_name!r}) timeout after {timeout_secs:.0f}s"
                await self._teardown()
                raise
            self.status.last_seen = time.time()
            return _serialise_call_result(result)

    # ----- internals ------------------------------------------------

    async def _run(self) -> None:
        """Supervisor loop: connect, wait for the connection to die,
        reconnect with backoff. Exits cleanly on `stop()`."""
        attempt = 0
        while not self._closing:
            try:
                await self._connect_once()
                # _connect_once returns when the connection drops cleanly
                # OR when an exception was raised. Either way we're now
                # disconnected. Reset counter — successful connect resets
                # the backoff sequence.
                attempt = 0
                if self._closing:
                    return
                # Connection ended naturally (rare for stdio backends —
                # usually means the child exited). Reconnect.
                self.status.status = STATUS_RECONNECTING
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self.status.last_error = str(exc)
                self.status.status = STATUS_RECONNECTING if attempt > 0 else STATUS_DOWN
                attempt += 1
                self.status.reconnect_attempt = attempt
            if self._closing:
                return
            delay = _RECONNECT_DELAYS[min(attempt, len(_RECONNECT_DELAYS) - 1)]
            self.status.next_retry_at = time.time() + delay
            log.info(
                "op-gateway: backend %r down (%s); retry in %.1fs (attempt %d)",
                self.defn.name, self.status.last_error, delay, attempt,
            )
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return

    async def _connect_once(self) -> None:
        """Connect, initialize, list tools, then block until the session
        ends. Cleanup is in finally so the AsyncExitStack always closes."""
        self.status.status = STATUS_CONNECTING
        self.status.next_retry_at = None
        params = StdioServerParameters(
            command=self.defn.command[0],
            args=list(self.defn.command[1:]),
            env=dict(self.defn.env) if self.defn.env else None,
            cwd=self.defn.cwd,
        )
        self._stack = AsyncExitStack()
        try:
            read, write = await self._stack.enter_async_context(stdio_client(params))
            self._session = await self._stack.enter_async_context(ClientSession(read, write))
            await self._session.initialize()
            tools_result = await self._session.list_tools()
            self._tools = list(tools_result.tools)
            self.status.status            = STATUS_UP
            self.status.started_at        = time.time()
            self.status.last_seen         = time.time()
            self.status.last_error        = None
            self.status.tool_count        = len(self._tools)
            self.status.reconnect_attempt = 0
            log.info(
                "op-gateway: backend %r up (%d tools)",
                self.defn.name, len(self._tools),
            )
            # Idle until cancellation or session-internal failure. The
            # session itself doesn't expose a "wait until closed" API,
            # so we sleep in a loop. If the underlying transport dies,
            # the next call_tool catches it and we tear down.
            while not self._closing:
                await asyncio.sleep(1.0)
        finally:
            await self._teardown()

    async def _teardown(self) -> None:
        """Close the session + transport. Safe to call multiple times."""
        if self._stack:
            try:
                await self._stack.aclose()
            except Exception:
                # Tearing down on top of a corrupted transport can throw;
                # we've already logged the cause upstream and there's
                # nothing more to do.
                pass
        self._stack = None
        self._session = None
        if self.status.status == STATUS_UP:
            self.status.status = STATUS_RECONNECTING


class BackendPool:
    """Collection of BackendConnections, keyed by backend name.

    Owned by the FastMCP server's lifespan. start_all on entry, stop_all
    on exit. Tool dispatch goes through `call_tool(namespace, tool_name,
    args)`.
    """

    def __init__(self, backends: list[BackendDef]) -> None:
        self._connections: dict[str, BackendConnection] = {
            b.name: BackendConnection(b) for b in backends
        }

    async def start_all(self) -> None:
        """Spin up every backend's supervisor task. Returns immediately
        — supervisors run in the background. Use `wait_for_initial_connect`
        if you need to block until they've all reached `up` (e.g. tests)."""
        for c in self._connections.values():
            await c.start()

    async def stop_all(self) -> None:
        """Cancel every supervisor + cleanly close every transport."""
        await asyncio.gather(
            *(c.stop() for c in self._connections.values()),
            return_exceptions=True,
        )

    async def wait_for_initial_connect(self, timeout_secs: float = 5.0) -> dict[str, bool]:
        """Block until every backend reaches `up`, or until the timeout.

        Returns a {backend_name: was_up} map so the caller can identify
        which backends failed. Used by tests that need a deterministic
        starting state."""
        results = await asyncio.gather(
            *(c.wait_until_up(timeout_secs) for c in self._connections.values()),
        )
        return dict(zip(self._connections.keys(), results))

    def get(self, name: str) -> BackendConnection | None:
        return self._connections.get(name)

    def names(self) -> list[str]:
        return list(self._connections.keys())

    def health(self) -> list[BackendStatus]:
        """Per-backend health snapshot. Used by the `health` meta-op."""
        return [c.status for c in self._connections.values()]

    async def call_tool(
        self,
        namespace: str,
        tool_name: str,
        arguments: dict[str, Any] | None,
        *,
        timeout_secs: float = DEFAULT_CALL_TIMEOUT_SECS,
    ) -> dict[str, Any]:
        """Route an op call to the named backend. Returns the backend's
        result in the FastMCP-friendly content-block shape, or raises
        BackendUnavailable if the backend isn't reachable."""
        conn = self._connections.get(namespace)
        if conn is None:
            raise BackendUnavailable(f"unknown namespace {namespace!r}")
        return await conn.call_tool(tool_name, arguments, timeout_secs=timeout_secs)

    def find_tool(self, namespace: str, tool_name: str) -> Any | None:
        """Look up a tool definition in the named backend's cached
        catalog. Returns the live MCP Tool object (with schema) or None
        if the backend doesn't expose that name."""
        conn = self._connections.get(namespace)
        if conn is None:
            return None
        for tool in conn.tools:
            if getattr(tool, "name", None) == tool_name:
                return tool
        return None


def _serialise_call_result(result: Any) -> dict[str, Any]:
    """Turn an MCP CallToolResult into a JSON-serialisable dict.

    The SDK returns a pydantic model; converting via `.model_dump` gives
    us the structured content blocks + isError flag in dict form, which
    the FastMCP layer above can hand back to Claude Code's SDK as the
    op-tool's result."""
    if hasattr(result, "model_dump"):
        return result.model_dump(mode="json")
    if isinstance(result, dict):
        return result
    # Defensive: stringify anything we don't recognise rather than crash.
    return {"content": [{"type": "text", "text": str(result)}], "isError": False}
