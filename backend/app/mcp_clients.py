"""Persistent stdio MCP sessions for deterministic tool calls (sweeps, sends).

Reads server definitions from ~/.claude/settings.json mcpServers and keeps
long-lived sessions. The Claude agent gets the same definitions separately via
the Agent SDK; this module is for direct, non-LLM calls.
"""

import json
import logging
import os
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .config import mcp_server_defs

log = logging.getLogger(__name__)


class MCPManager:
    def __init__(self, server_names: list[str]):
        self.server_names = server_names
        self.sessions: dict[str, ClientSession] = {}
        self._stack = AsyncExitStack()

    async def start(self) -> dict[str, str]:
        """Start each server; returns {name: 'ok' | error string}."""
        defs = mcp_server_defs()
        status = {}
        for name in self.server_names:
            cfg = defs.get(name)
            if not cfg:
                status[name] = "not defined in settings.json"
                continue
            try:
                env = {**os.environ, **cfg.get("env", {})}
                params = StdioServerParameters(
                    command=cfg["command"], args=cfg.get("args", []), env=env
                )
                read, write = await self._stack.enter_async_context(stdio_client(params))
                session = await self._stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                self.sessions[name] = session
                status[name] = "ok"
            except Exception as e:
                log.exception("MCP server %s failed to start", name)
                status[name] = f"error: {e}"
        return status

    async def call(self, server: str, tool: str, args: dict | None = None):
        """Call a tool; returns parsed JSON when the result is JSON, else raw text."""
        session = self.sessions.get(server)
        if session is None:
            raise RuntimeError(f"MCP server '{server}' is not connected")
        result = await session.call_tool(tool, args or {})
        texts = [c.text for c in result.content if getattr(c, "type", None) == "text"]
        text = "\n".join(texts)
        if result.isError:
            raise RuntimeError(f"{server}.{tool}: {text[:500]}")
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return text

    async def stop(self):
        await self._stack.aclose()
        self.sessions.clear()


manager: MCPManager | None = None


def get_manager() -> MCPManager:
    if manager is None:
        raise RuntimeError("MCP manager not started")
    return manager
