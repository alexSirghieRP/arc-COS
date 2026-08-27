"""Automatic MS Graph re-authentication (owner decision, 2026-07-14).

Interactive sign-in can't be automated away: auth-node.js still opens a real
Microsoft login page and the user has to click through it themselves (typing a
password or approving MFA is not something this agent will ever do on their
behalf). What CAN be automated is everything BEFORE that click: noticing the
token is dead (job_runs already carries this signal for the board's banner),
finding a free localhost port for the OAuth redirect (auth-node.js's
AUTH_PORT default of 3000 collides with other local dev servers most days),
and launching the script through the same open_terminal() the desktop agent
already uses for long-running processes. By the time the user sees the toast,
the only thing left for them to do is sign in.
"""

import logging
import socket
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .config import mcp_server_defs, policy
from .connectors import recent_graph_auth_failure

log = logging.getLogger("chief.graph_reauth")

_PORT_CANDIDATES = range(3000, 3011)
DEFAULT_COOLDOWN_MINUTES = 60
DEFAULT_MAX_INFLIGHT_MINUTES = 30


def _free_port() -> int | None:
    # Node's server.listen(port) (no host arg, what auth-node.js uses) binds
    # the IPv6 wildcard by default - probing IPv4 loopback alone misses a
    # conflict there (verified: a busy IPv6-wildcard port still let an IPv4
    # loopback bind succeed, then Node's own listen() hit EADDRINUSE anyway).
    for port in _PORT_CANDIDATES:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("::", port))
            except OSError:
                continue
            return port
    return None


def _graph_dir() -> Path | None:
    args = mcp_server_defs().get("ms-graph", {}).get("args") or []
    return Path(args[0]).resolve().parent if args else None


async def maybe_reauth() -> dict:
    """Launch auth-node.js iff the graph token looks dead, no attempt is
    already in flight, and we're past the cooldown since the last one."""
    cfg = policy().get("graph_reauth", {}) or {}
    if not cfg.get("enabled", True):
        return {"skipped": "graph_reauth.enabled is false"}
    if not recent_graph_auth_failure():
        return {"idle": True}

    from . import desktop
    session = db.get_setting("graph_reauth_session")
    if session:
        live = next((s for s in desktop.list_terminals() if s["id"] == session), None)
        if live and live.get("running"):
            # auth-node.js blocks until the user clicks through a real Microsoft
            # login page. When they don't, the process sits there forever and
            # pins this lock -- which silently disables re-auth for as long as
            # it runs, with no error anywhere: the job just keeps reporting
            # "already in flight". Observed 2026-08: a sign-in launched on the
            # 19th was still holding the lock on the 24th, so every Graph sweep
            # stayed dead for three days behind a healthy-looking job. A sign-in
            # nobody completed within max_inflight_minutes is abandoned, not in
            # flight -- stop it and fall through to the cooldown check, which
            # decides on its own whether enough time has passed to relaunch.
            max_inflight = int(cfg.get("max_inflight_minutes",
                                       DEFAULT_MAX_INFLIGHT_MINUTES))
            if (live.get("started") or "") > db.cutoff(minutes=max_inflight):
                return {"skipped": "re-auth already in flight", "session": session}
            await desktop.terminal_stop(session)
            db.audit("graph_reauth_abandoned",
                     {"session": session, "started": live.get("started"),
                      "max_inflight_minutes": max_inflight})
            log.warning("graph_reauth: abandoning stale sign-in %s (started %s, "
                        "limit %dm)", session, live.get("started"), max_inflight)
        # Dead, missing (backend restarted -- sessions are in-memory), or just
        # abandoned above: the id is stale either way, so stop consulting it.
        db.set_setting("graph_reauth_session", "")

    last = db.get_setting("graph_reauth_last_attempt")
    cooldown = int(cfg.get("cooldown_minutes", DEFAULT_COOLDOWN_MINUTES))
    if last:
        elapsed_min = (datetime.now(timezone.utc)
                       - datetime.fromisoformat(last)).total_seconds() / 60
        if elapsed_min < cooldown:
            return {"skipped": f"cooldown ({elapsed_min:.0f}/{cooldown}m)"}

    graph_dir = _graph_dir()
    if not graph_dir or not (graph_dir / "auth-node.js").exists():
        return {"skipped": "mcp-ms-graph / auth-node.js not found"}

    port = _free_port()
    if port is None:
        return {"skipped": "no free port in 3000-3010"}

    result = await desktop.open_terminal(graph_dir.name, f"AUTH_PORT={port} node auth-node.js")
    if result.get("needs_confirm"):
        # classify() should never flag this as high-risk, but never hang
        # waiting on a confirmation nobody knows to send - surface it instead
        log.warning("graph_reauth: open_terminal asked for confirmation: %s", result)
        return {"error": "auth-node.js classified high-risk, needs manual confirm", **result}

    db.set_setting("graph_reauth_session", result["session"])
    db.set_setting("graph_reauth_last_attempt", db.now())
    db.audit("graph_reauth_started", {"port": port, "session": result["session"]})
    log.info("graph_reauth: launched auth-node.js on port %d (session %s)",
             port, result["session"])
    return {"started": True, "port": port, "session": result["session"]}
