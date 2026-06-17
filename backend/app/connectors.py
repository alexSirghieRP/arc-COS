"""Connector registry: one place that knows what Chief can reach.

A connector is anything with a name and an async status() -> dict with at least
{"connected": bool, "detail": str}. New integrations (Salesforce, Jira, ...)
register here; nothing else in the app needs to change to show them on the board.
"""

import asyncio
from pathlib import Path
from typing import Awaitable, Callable

from . import mcp_clients
from .config import mcp_server_defs, vault_root

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


def _mcp_status(server: str) -> dict:
    try:
        mgr = mcp_clients.get_manager()
    except RuntimeError:
        return {"connected": False, "detail": "MCP manager not started"}
    if server in mgr.sessions:
        return {"connected": True, "detail": "MCP session live"}
    defined = server in mcp_server_defs()
    return {"connected": False,
            "detail": "defined in settings.json, not started" if defined else "not configured"}


@register("ms-graph")
async def ms_graph():
    return _mcp_status("ms-graph")


@register("azure-devops")
async def azure_devops():
    return _mcp_status("azure-devops")


@register("confluence")
async def confluence():
    return _mcp_status("atlassian")


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
