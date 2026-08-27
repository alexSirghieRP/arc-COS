"""Connector registry: one place that knows what Chief can reach.

A connector is anything with a name and an async status() -> dict with at least
{"connected": bool, "detail": str}. New integrations (Salesforce, Jira, ...)
register here; nothing else in the app needs to change to show them on the board.
"""

import asyncio
from pathlib import Path
from typing import Awaitable, Callable

from . import db, mcp_clients
from .config import mcp_server_defs, vault_root

_AUTH_FAIL_PATTERNS = ("fetch failed", "not logged in", "unauthenticated",
                       "invalid_grant", "token expired", "unauthorized",
                       "no valid access token")


def recent_graph_auth_failure() -> str | None:
    """Return the most recent auth-style error from ms-graph-backed jobs, or None."""
    rows = db.query(
        "SELECT error, started_at FROM job_runs "
        "WHERE job IN ('teams_sweep','email_sweep') AND ok=0 AND error IS NOT NULL "
        "ORDER BY id DESC LIMIT 20")
    for r in rows:
        err = (r["error"] or "").lower()
        if any(p in err for p in _AUTH_FAIL_PATTERNS):
            return r["error"]
    return None

Connector = Callable[[], Awaitable[dict]]
_registry: dict[str, Connector] = {}


def register(name: str):
    def deco(fn: Connector):
        _registry[name] = fn
        return fn
    return deco


async def statuses() -> dict[str, dict]:
    names = list(_registry)
    results = await asyncio.gather(*(_registry[n]() for n in names), return_exceptions=True)
    return {
        n: (r if isinstance(r, dict) else {"connected": False, "detail": str(r)[:120]})
        for n, r in zip(names, results)
    }


async def _mcp_status(server: str) -> dict:
    try:
        mgr = mcp_clients.get_manager()
    except RuntimeError:
        return {"connected": False, "detail": "MCP manager not started"}
    if server in mgr.sessions:
        probe = await mgr.probe(server)
        stats = mgr.stats().get(server, {})
        if not probe.get("alive"):
            # stats["connected"] is just "session object still registered" (True even
            # for a hung session) — spread it first so the probe result below wins.
            return {**stats, "connected": False, "reconnectable": True,
                    "detail": f"session unresponsive: {probe.get('detail', '?')}"}
        latency = probe.get("latency_ms")
        return {**stats, "connected": True, "reconnectable": True, "latency_ms": latency,
                "detail": f"live · ping {latency}ms"}
    defined = server in mcp_server_defs()
    return {"connected": False, "reconnectable": defined,
            "detail": "defined in settings.json, not started" if defined else "not configured"}


@register("ms-graph")
async def ms_graph():
    status = await _mcp_status("ms-graph")
    if status.get("connected"):
        auth_err = recent_graph_auth_failure()
        if auth_err:
            status["auth_warning"] = True
            status["detail"] = f"session live but token may be expired — {auth_err[:80]}"
            status["last_error"] = auth_err
    return status


@register("azure-devops")
async def azure_devops():
    return await _mcp_status("azure-devops")


@register("confluence")
async def confluence():
    # Confluence is reached over REST with the atlassian token, not an MCP
    # session, so health = credentials present (calls are cheap-checked there).
    env = mcp_server_defs().get("atlassian", {}).get("env", {})
    ok = bool(env.get("ATLASSIAN_URL") and env.get("ATLASSIAN_EMAIL")
              and env.get("ATLASSIAN_API_TOKEN"))
    return {"connected": ok,
            "detail": "REST token configured" if ok else "no Atlassian credentials in settings.json"}


@register("github")
async def github():
    proc = await asyncio.create_subprocess_exec(
        "gh", "auth", "status",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    ok = await proc.wait() == 0
    return {"connected": ok, "detail": "gh CLI" if ok else "gh auth status failed"}


@register("obsidian")
async def obsidian():
    root = vault_root()
    ok = root.exists()
    return {"connected": ok, "detail": str(root) if ok else f"vault missing: {root}"}


@register("salesforce")
async def salesforce():
    # Stub by design: no Salesforce connector exists yet. When one does, replace
    # this with a real health check and the rest of the app picks it up as-is.
    return {"connected": False, "detail": "not connected (no connector built yet)"}
