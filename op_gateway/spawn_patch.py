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
        Spawn directly via subprocess.Popen + CREATE_NO_WINDOW; wrap
        in `FallbackProcess` so the rest of `stdio_client` works
        unchanged. Bypassing MCP SDK's own `_create_windows_fallback_process`
        is intentional — that function has a try/except that silently
        falls back to a no-creationflags Popen on the first failure,
        undoing the workaround."""
        if errlog is None:
            errlog = sys.stderr
        # Do the spawn DIRECTLY here (don't route through MCP SDK's
        # `_create_windows_fallback_process` — that has its own
        # try/except that silently falls back to a NO-creationflags
        # Popen call if the first one raises, undoing the whole
        # workaround). Use the same FallbackProcess wrapping the SDK
        # uses so the rest of `stdio_client` works unchanged.
        import subprocess as _sp
        # Coerce errlog to a usable subprocess stderr target. Popen
        # accepts file descriptors, PIPE, DEVNULL, or file objects
        # with .fileno(). If errlog is a custom stream wrapping a
        # JSON-RPC pipe (no .fileno()), route to DEVNULL so the spawn
        # succeeds with CREATE_NO_WINDOW intact.
        safe_errlog: object = errlog
        try:
            fd = errlog.fileno() if hasattr(errlog, "fileno") else None
            if fd is None or fd < 0:
                safe_errlog = _sp.DEVNULL
        except (OSError, ValueError):
            safe_errlog = _sp.DEVNULL
        try:
            popen_obj = _sp.Popen(
                [command, *args],
                stdin=_sp.PIPE,
                stdout=_sp.PIPE,
                stderr=safe_errlog,
                env=env,
                cwd=cwd,
                bufsize=0,
                creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            # Fail loudly — don't silently spawn a console-attached
            # backend. The caller will see this and the user can
            # report it.
            print(
                f"spawn_patch: subprocess.Popen({command}) raised "
                f"{type(exc).__name__}: {exc} — re-raising rather "
                f"than silently spawning with a conhost",
                file=sys.stderr, flush=True,
            )
            raise
        process = _mcp_win.FallbackProcess(popen_obj)
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
    # Defensive: server.py calls apply() at module import time, BEFORE
    # logging.basicConfig runs in main(), so the log.info above gets
    # dropped on the floor (no handler attached yet). Mirror to stderr
    # directly so we can prove from the gateway's captured stderr that
    # the patch actually applied.
    print(
        "spawn_patch: monkey-patched create_windows_process in "
        "mcp.os.win32.utilities AND mcp.client.stdio to skip the buggy "
        "anyio path (no conhost.exe per backend)",
        file=sys.stderr, flush=True,
    )
    return True
