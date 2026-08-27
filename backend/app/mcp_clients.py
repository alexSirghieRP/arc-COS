"""Persistent stdio MCP sessions for deterministic tool calls (sweeps, sends).

Reads server definitions from ~/.claude/settings.json mcpServers and keeps
long-lived sessions. The Claude agent gets the same definitions separately via
the Agent SDK; this module is for direct, non-LLM calls.

Every call is timed and counted per server (calls, errors, latency, last
error) so the board's Health tab can show real connector health, and a slow
or hung server can never wedge a sweep: calls time out after CALL_TIMEOUT_S
and the session is reconnected on the next use.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import time
from contextlib import AsyncExitStack

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .config import mcp_server_defs

log = logging.getLogger(__name__)

# Errors that mean the stdio session/subprocess is gone and must be reconnected.
_DEAD_SESSION = (anyio.ClosedResourceError, anyio.BrokenResourceError, ConnectionError)

CALL_TIMEOUT_S = 120  # hard ceiling per tool call; a hung server must not wedge sweeps
PROBE_TIMEOUT_S = 5


def _new_stats() -> dict:
    return {"calls": 0, "errors": 0, "total_ms": 0, "last_ok": None,
            "last_error": None, "last_error_ts": None, "connected_since": None}


def _nvm_node_bin() -> str | None:
    """Directory holding nvm's node, or None when nvm isn't installed there.
    Prefers the version nvm's own `default` alias points at, else the newest
    installed one (compared numerically - a string sort puts v9 above v24)."""
    nvm = os.path.expanduser(os.environ.get("NVM_DIR") or "~/.nvm")
    versions_dir = os.path.join(nvm, "versions", "node")
    if not os.path.isdir(versions_dir):
        return None
    installed = [v for v in os.listdir(versions_dir)
                 if os.path.isfile(os.path.join(versions_dir, v, "bin", "node"))]
    if not installed:
        return None

    def _bin(version: str) -> str:
        return os.path.join(versions_dir, version, "bin")

    try:
        with open(os.path.join(nvm, "alias", "default")) as f:
            alias = f.read().strip()
    except OSError:
        alias = ""
    if alias:
        # the alias file holds anything from "v24.14.1" to "24" to "lts/*";
        # only an exact version match is trusted, the rest fall through
        for v in installed:
            if alias in (v, v.lstrip("v")):
                return _bin(v)
    return _bin(max(installed, key=lambda v: tuple(int(n) for n in re.findall(r"\d+", v))))


def _spawn_path(base: str) -> str:
    """PATH for MCP server subprocesses.

    settings.json spawns servers as bare `node`/`npx` (ms-graph, obsidian,
    atlassian), but nvm keeps node under ~/.nvm/versions/node/<v>/bin and is
    loaded only from .zshrc - which non-interactive shells never source. A
    backend started from the macOS app (`zsh -lc`) or the launchd plist (whose
    PATH is hardcoded) therefore inherits no node at all, and every stdio spawn
    dies with FileNotFoundError before a single tool call (observed 2026-08-11:
    ms-graph was down this way for hours - every Graph sweep, pr_channel_review
    included, failed in ~20ms, so a PR review request sat untouched all day).

    Only ever APPENDS, and only when node can't already be resolved, so a
    backend started from a terminal keeps whatever node its shell picked."""
    if shutil.which("node", path=base):
        return base
    bin_dir = _nvm_node_bin()
    return f"{base}{os.pathsep}{bin_dir}" if bin_dir else base


class MCPManager:
    def __init__(self, server_names: list[str]):
        self.server_names = server_names
        self.sessions: dict[str, ClientSession] = {}
        # Per-server exit stacks so one dead server can be torn down and
        # reconnected without disturbing the others.
        self._stacks: dict[str, AsyncExitStack] = {}
        self._stats: dict[str, dict] = {n: _new_stats() for n in server_names}

    def _stat(self, name: str) -> dict:
        return self._stats.setdefault(name, _new_stats())

    async def _connect(self, name: str) -> str:
        """(Re)establish a single server's stdio session. Returns 'ok' or error."""
        cfg = mcp_server_defs().get(name)
        if not cfg:
            return "not defined in settings.json"
        stack = AsyncExitStack()
        try:
            env = {**os.environ, **cfg.get("env", {})}
            env["PATH"] = _spawn_path(env.get("PATH") or os.defpath)
            params = StdioServerParameters(
                command=cfg["command"], args=cfg.get("args", []), env=env
            )
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self.sessions[name] = session
            self._stacks[name] = stack
            self._stat(name)["connected_since"] = time.time()
            return "ok"
        except Exception as e:
            log.exception("MCP server %s failed to start", name)
            self._record_error(name, f"connect: {e}")
            try:
                await stack.aclose()
            except Exception:
                log.warning("error closing failed MCP stack for %s", name, exc_info=True)
            return f"error: {e}"

    async def _reconnect(self, name: str) -> str:
        """Tear down a stale session (subprocess likely already dead) and reconnect."""
        self.sessions.pop(name, None)
        self._stat(name)["connected_since"] = None
        old = self._stacks.pop(name, None)
        if old is not None:
            try:
                await old.aclose()
            except Exception as e:
                # The subprocess is already gone; cross-task cancel-scope noise
                # from aclose is expected here and safe to drop. One line at
                # WARNING, full traceback only at DEBUG (keeps the Logs tab sane).
                log.warning("closing stale MCP stack for %s: %s", name, str(e)[:120])
                log.debug("stale MCP stack close traceback for %s", name, exc_info=True)
        return await self._connect(name)

    async def reconnect(self, name: str) -> str:
        """Public restart, e.g. from the board's Health tab."""
        return await self._reconnect(name)

    async def start(self) -> dict[str, str]:
        """Start each server; returns {name: 'ok' | error string}."""
        return {name: await self._connect(name) for name in self.server_names}

    def _record_error(self, server: str, message: str) -> None:
        s = self._stat(server)
        s["errors"] += 1
        s["last_error"] = str(message)[:300]
        s["last_error_ts"] = time.time()

    async def call(self, server: str, tool: str, args: dict | None = None,
                   timeout: float = CALL_TIMEOUT_S):
        """Call a tool; returns parsed JSON when the result is JSON, else raw text.
        Reconnects once if the session has died since the last call."""
        session = self.sessions.get(server)
        if session is None:
            status = await self._reconnect(server)
            if status != "ok":
                raise RuntimeError(f"MCP server '{server}' is not connected ({status})")
            session = self.sessions[server]
        stats = self._stat(server)
        stats["calls"] += 1
        t0 = time.monotonic()
        try:
            try:
                result = await asyncio.wait_for(
                    session.call_tool(tool, args or {}), timeout=timeout)
            except _DEAD_SESSION:
                log.warning("MCP %s session closed; reconnecting and retrying", server)
                status = await self._reconnect(server)
                if status != "ok":
                    raise RuntimeError(f"MCP server '{server}' reconnect failed ({status})")
                # Retry of the same logical call — don't double-count it in stats["calls"].
                result = await asyncio.wait_for(
                    self.sessions[server].call_tool(tool, args or {}), timeout=timeout)
            except asyncio.TimeoutError:
                # The pending request is now orphaned on that session; drop the
                # session so the next call starts clean instead of piling up.
                self.sessions.pop(server, None)
                raise RuntimeError(
                    f"{server}.{tool}: timed out after {timeout:.0f}s (session reset)")
        except Exception as e:
            self._record_error(server, f"{tool}: {e}")
            raise
        finally:
            stats["total_ms"] += int((time.monotonic() - t0) * 1000)

        texts = [c.text for c in result.content if getattr(c, "type", None) == "text"]
        text = "\n".join(texts)
        if result.isError:
            self._record_error(server, f"{tool}: {text[:200]}")
            raise RuntimeError(f"{server}.{tool}: {text[:500]}")
        stats["last_ok"] = time.time()
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return text

    async def probe(self, server: str) -> dict:
        """Cheap liveness check: MCP ping with a short timeout."""
        session = self.sessions.get(server)
        if session is None:
            return {"alive": False, "detail": "no session"}
        t0 = time.monotonic()
        try:
            await asyncio.wait_for(session.send_ping(), timeout=PROBE_TIMEOUT_S)
            return {"alive": True, "latency_ms": int((time.monotonic() - t0) * 1000)}
        except Exception as e:
            return {"alive": False, "detail": str(e)[:200]}

    def stats(self) -> dict[str, dict]:
        """Per-server health counters for /api/health and the connectors bar."""
        out = {}
        for name in self.server_names:
            s = self._stat(name)
            out[name] = {
                "connected": name in self.sessions,
                "connected_since": s["connected_since"],
                "calls": s["calls"],
                "errors": s["errors"],
                "avg_latency_ms": int(s["total_ms"] / s["calls"]) if s["calls"] else None,
                "last_ok": s["last_ok"],
                "last_error": s["last_error"],
                "last_error_ts": s["last_error_ts"],
            }
        return out

    async def stop(self):
        for name in list(self._stacks):
            stack = self._stacks.pop(name, None)
            if stack is not None:
                try:
                    await stack.aclose()
                except Exception:
                    log.warning("error closing MCP stack for %s", name, exc_info=True)
        self.sessions.clear()


manager: MCPManager | None = None


def get_manager() -> MCPManager:
    if manager is None:
        raise RuntimeError("MCP manager not started")
    return manager
