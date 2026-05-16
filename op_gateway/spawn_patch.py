"""Workaround for an MCP SDK bug on Windows: the SDK's primary spawn
path (`anyio.open_process` with `creationflags=CREATE_NO_WINDOW`)
allocates a conhost.exe window per spawned backend ANYWAY, despite the
flag being passed correctly. The result for the user is a barrage of
~10 console windows flashing on their desktop every time a Claude Code
chat starts (one per backend the gateway spawns).

The MCP SDK's FALLBACK path (`_create_windows_fallback_process`, which
uses raw `subprocess.Popen` with the same CREATE_NO_WINDOW flag)
correctly suppresses the conhost. The bug is purely in anyio's Windows
process-transport setup — confirmed empirically:

    # anyio.open_process(..., creationflags=CREATE_NO_WINDOW)
    # → conhost child IS created  ✗

    # subprocess.Popen(..., creationflags=CREATE_NO_WINDOW)
    # → no conhost child            ✓

This module monkey-patches `mcp.os.win32.utilities.create_windows_process`
to skip the buggy primary path and go straight to the working fallback.
Apply once at module-load time (call `apply()` from `op_gateway.server`).

No-op on POSIX. Idempotent — re-applying is safe.

Long-term fix is to file an issue / PR against
https://github.com/modelcontextprotocol/python-sdk to either fix
anyio's open_process behaviour or remove the buggy primary path. Until
that ships, this workaround makes the dev UX bearable.
"""
from __future__ import annotations

import logging
import sys


log = logging.getLogger(__name__)


_PATCH_APPLIED = False


def apply() -> bool:
    """Install the spawn-path patch. Returns True on success, False on
    no-op (non-Windows, already applied, or MCP SDK not importable).

    Idempotent — multiple calls are safe."""
    global _PATCH_APPLIED
    if _PATCH_APPLIED:
        return False
    if sys.platform != "win32":
        return False

    try:
        from mcp.os.win32 import utilities as _mcp_win
    except ImportError:
        log.debug(
            "spawn_patch: mcp.os.win32.utilities not importable — "
            "skipping patch (likely older MCP SDK)"
        )
        return False

    if not hasattr(_mcp_win, "_create_windows_fallback_process"):
        log.warning(
            "spawn_patch: mcp.os.win32.utilities lacks "
            "`_create_windows_fallback_process` — SDK shape changed; "
            "patch not applied. Conhost popups will continue until "
            "the upstream issue is addressed."
        )
        return False
    if not hasattr(_mcp_win, "create_windows_process"):
        log.warning(
            "spawn_patch: mcp.os.win32.utilities lacks "
            "`create_windows_process` — SDK shape changed; patch not "
            "applied."
        )
        return False

    _original = _mcp_win.create_windows_process

    async def _patched_create_windows_process(
        command: str,
        args: list,
        env: dict | None = None,
        errlog=None,
        cwd=None,
    ):
        """Skip the buggy anyio.open_process primary path entirely.
        The fallback (raw subprocess.Popen with CREATE_NO_WINDOW) works
        correctly. Job-Object wiring still happens because we route
        through the SDK's own fallback function."""
        if errlog is None:
            errlog = sys.stderr
        # The fallback returns a `FallbackProcess` which the SDK
        # already handles correctly downstream (its stdio_client
        # interface treats it identically to a native anyio Process).
        process = await _mcp_win._create_windows_fallback_process(
            command, args, env, errlog, cwd,
        )
        # The SDK normally wires the process to a Job Object inside
        # `create_windows_process` (see `_maybe_assign_process_to_job`).
        # Mirror that for the fallback path too so child-tree cleanup
        # still works on SDK-driven shutdown.
        try:
            job = _mcp_win._create_job_object()
            _mcp_win._maybe_assign_process_to_job(process, job)
        except Exception:
            log.exception(
                "spawn_patch: Job Object assignment raised; "
                "continuing without it (backend tree won't auto-kill on "
                "gateway exit, but the spawn itself succeeded)"
            )
        return process

    _mcp_win.create_windows_process = _patched_create_windows_process  # type: ignore[assignment]

    # IMPORTANT: `mcp.client.stdio` does `from mcp.os.win32.utilities import
    # create_windows_process` AT IMPORT TIME, and its
    # `_create_platform_compatible_process` (the function `stdio_client`
    # actually calls) references `create_windows_process` as a BARE NAME
    # resolved from the `mcp.client.stdio` module namespace. So patching
    # only `mcp.os.win32.utilities` leaves `mcp.client.stdio`'s own
    # reference still bound to the original buggy function. Patch both
    # explicitly — same target function on each — and add any other
    # site we discover.
    try:
        from mcp.client import stdio as _mcp_stdio
        if hasattr(_mcp_stdio, "create_windows_process"):
            _mcp_stdio.create_windows_process = _patched_create_windows_process  # type: ignore[assignment]
    except ImportError:
        # `mcp.client.stdio` not importable on this install — primary
        # patch above still covers any other caller that does
        # `from mcp.os.win32.utilities import create_windows_process`
        # at call time (rather than import time).
        pass

    _PATCH_APPLIED = True
    log.info(
        "spawn_patch: monkey-patched create_windows_process in "
        "mcp.os.win32.utilities AND mcp.client.stdio to use the "
        "subprocess.Popen fallback path (avoids conhost.exe windows on "
        "each backend spawn — MCP SDK upstream bug)"
    )
    return True
