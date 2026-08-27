"""SSE broadcast channel. Zero-import cycle: both api.py and health.py import from here."""
import asyncio
import json
import logging

log = logging.getLogger("chief.events")

_clients: set[asyncio.Queue] = set()


def broadcast(event_type: str, data: dict | None = None) -> None:
    """Push an SSE event to every connected browser tab. Fire-and-forget."""
    if not _clients:
        return
    payload = json.dumps({"type": event_type, **(data or {})}, ensure_ascii=False, default=str)
    dead: set[asyncio.Queue] = set()
    for q in _clients:
        try:
            q.put_nowait(payload)
        except (asyncio.QueueFull, RuntimeError):
            dead.add(q)
    for q in dead:
        _clients.discard(q)


def register(q: asyncio.Queue) -> None:
    _clients.add(q)


def unregister(q: asyncio.Queue) -> None:
    _clients.discard(q)
