"""Microsoft Graph helpers over the ms-graph MCP server."""

import re
from datetime import date, datetime, timedelta, timezone

from .mcp_clients import get_manager

SERVER = "ms-graph"


async def presence() -> dict:
    return await get_manager().call(SERVER, "graph_get_presence")


async def list_emails(top: int = 10, time_filter: str | None = None) -> list[dict]:
    args: dict = {"top": top}
    if time_filter:
        args["timeFilter"] = time_filter
    out = await get_manager().call(SERVER, "graph_list_emails", args)
    return out if isinstance(out, list) else []


async def calendar_events(day: date) -> list[dict]:
    """Events for a local calendar day, queried in UTC with margin and filtered locally."""
    local_tz = datetime.now().astimezone().tzinfo
    start_local = datetime.combine(day, datetime.min.time(), tzinfo=local_tz)
    end_local = start_local + timedelta(days=1)
    out = await get_manager().call(SERVER, "graph_list_calendar_events", {
        "startDateTime": start_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endDateTime": end_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "top": 50,
        "selectFields": "id,subject,start,end,location,organizer,attendees,isAllDay,showAs,webLink",
    })
    events = out.get("events", []) if isinstance(out, dict) else []
    for e in events:
        for key in ("start", "end"):
            v = e.get(key, {})
            if v.get("dateTime"):
                dt = datetime.fromisoformat(v["dateTime"].split(".")[0]).replace(tzinfo=timezone.utc)
                e[key + "_local"] = dt.astimezone(local_tz).isoformat()
    return events


async def list_chats(top: int = 50) -> list[dict]:
    out = await get_manager().call(SERVER, "graph_list_chats", {"top": top, "expand": True})
    return out if isinstance(out, list) else []


_chats_cache: list[dict] = []
_chats_ts: float = 0.0
_chats_lock: "asyncio.Lock | None" = None


def _get_chats_lock() -> "asyncio.Lock":
    global _chats_lock
    if _chats_lock is None:
        import asyncio
        _chats_lock = asyncio.Lock()
    return _chats_lock


async def chats_cached(ttl: int = 900) -> list[dict]:
    """Return cached chat list; serializes concurrent fetches so the MCP
    stdio connection is never hammered with duplicate list_chats calls."""
    global _chats_cache, _chats_ts
    import asyncio, time
    if time.time() - _chats_ts <= ttl:
        return _chats_cache  # fast path: cache is warm
    async with _get_chats_lock():
        # Re-check after acquiring lock (another coroutine may have fetched already).
        if time.time() - _chats_ts <= ttl:
            return _chats_cache
        _chats_cache = await list_chats(top=50)
        _chats_ts = time.time()
    return _chats_cache


async def warm_caches() -> None:
    """Proactively refresh chats + today's + yesterday's calendar before they expire.
    Called by the scheduler every ~5 minutes so sweeps never wait for a cold
    cache fetch. Silent: errors don't propagate."""
    import asyncio
    import time
    from datetime import date as _date, timedelta as _td

    async def _warm_chats():
        try:
            if time.time() - _chats_ts > 780:  # 900-120
                await chats_cached()
        except Exception:
            pass

    async def _warm_cal(day):
        try:
            key = day.isoformat()
            if time.time() - _calendar_ts.get(key, 0) > 480:  # 600-120
                await calendar_events_cached(day)
        except Exception:
            pass

    today = _date.today()

    async def _warm_presence():
        try:
            from . import state as _state
            if _state._presence_ts == 0 or time.time() - _state._presence_ts > 100:
                await _state.get_presence()
        except Exception:
            pass

    await asyncio.gather(
        _warm_chats(),
        _warm_cal(today),
        _warm_cal(today - _td(days=1)),
        _warm_presence(),
    )


_calendar_cache: dict[str, list] = {}  # date_iso -> events
_calendar_ts: dict[str, float] = {}
_calendar_locks: dict[str, "asyncio.Lock"] = {}


async def calendar_events_cached(day: date, ttl: int = 600) -> list[dict]:
    """calendar_events with a 10-minute in-process cache per day, serialized
    per-day so concurrent sweeps never hammer the MCP server simultaneously."""
    import asyncio, time
    key = day.isoformat()
    if time.time() - _calendar_ts.get(key, 0) <= ttl:
        return _calendar_cache.get(key, [])  # fast path: cache warm
    if key not in _calendar_locks:
        _calendar_locks[key] = asyncio.Lock()
    async with _calendar_locks[key]:
        if time.time() - _calendar_ts.get(key, 0) <= ttl:
            return _calendar_cache.get(key, [])  # another coroutine fetched while we waited
        _calendar_cache[key] = await calendar_events(day)
        _calendar_ts[key] = time.time()
    return _calendar_cache[key]


async def read_chat_messages(chat_id: str, top: int = 20) -> list[dict]:
    out = await get_manager().call(SERVER, "graph_read_chat_messages", {"chatId": chat_id, "top": top})
    if isinstance(out, dict):
        return out.get("messages", out.get("value", []))
    return out if isinstance(out, list) else []


import html as _html

_HTML_TAG = re.compile(r"</?(p|ul|ol|li|br|a|b|strong|em|i|h[1-6])\b", re.I)
_URL = re.compile(r"(https?://[^\s<]+)")
_URL_TRAILING_PUNCT = ".,;:!?]}'\""


def _linkify(m: "re.Match") -> str:
    """Wrap the matched URL in <a>, peeling off trailing sentence punctuation
    (and an unmatched closing paren) so it isn't swallowed into the href."""
    url = m.group(1)
    trail = ""
    while url and url[-1] in _URL_TRAILING_PUNCT:
        trail = url[-1] + trail
        url = url[:-1]
    if url.endswith(")") and url.count("(") < url.count(")"):
        trail = ")" + trail
        url = url[:-1]
    return f'<a href="{url}">{url}</a>{trail}'


def to_html(body: str) -> str:
    """Teams renders chat/channel messages as HTML, so plain text with newlines
    collapses into a wall of text and bare URLs don't link. Convert plain text
    to paragraphs/<br> with auto-linked URLs; leave already-HTML bodies alone."""
    if not body or _HTML_TAG.search(body):
        return body
    esc = _html.escape(body)
    esc = _URL.sub(_linkify, esc)
    paras = [p.strip() for p in esc.split("\n\n") if p.strip()]
    return "".join(f"<p>{p.replace(chr(10), '<br>')}</p>" for p in paras)


async def send_chat_message(chat_id: str, message: str) -> dict:
    return await get_manager().call(SERVER, "graph_send_chat_message", {
        "chatId": chat_id, "message": to_html(message),
    })


async def get_chat_members(chat_id: str) -> list[dict]:
    out = await get_manager().call(SERVER, "graph_get_chat_members", {"chatId": chat_id})
    if isinstance(out, list):
        return out
    return out.get("members", out.get("value", [])) if isinstance(out, dict) else []


async def create_chat_with_user(email: str) -> dict:
    """Create or get the 1:1 chat with a user by email. Passing the user's own
    email returns their Teams self-chat (idempotent)."""
    out = await get_manager().call(SERVER, "graph_create_chat_with_user", {"userEmail": email})
    return out if isinstance(out, dict) else {}


async def search_users(name: str) -> list[dict]:
    out = await get_manager().call(SERVER, "graph_search_users", {"displayName": name})
    return out if isinstance(out, list) else out.get("value", []) if isinstance(out, dict) else []


async def read_channel_messages(team_id: str, channel_id: str, top: int = 20) -> list[dict]:
    out = await get_manager().call(SERVER, "graph_read_channel_messages", {
        "teamId": team_id, "channelId": channel_id, "top": top})
    if isinstance(out, dict):
        return out.get("messages", out.get("value", []))
    return out if isinstance(out, list) else []


async def post_channel_message(team_id: str, channel_id: str, message: str,
                               reply_to: str | None = None) -> dict:
    args = {"teamId": team_id, "channelId": channel_id, "message": to_html(message)}
    if reply_to:
        args["replyToMessageId"] = reply_to
    return await get_manager().call(SERVER, "graph_post_channel_message", args)


async def read_channel_message_replies(team_id: str, channel_id: str, message_id: str,
                                       top: int = 50) -> list[dict]:
    out = await get_manager().call(SERVER, "graph_read_channel_message_replies", {
        "teamId": team_id, "channelId": channel_id, "messageId": message_id, "top": top})
    if isinstance(out, dict):
        return out.get("replies", out.get("value", []))
    return out if isinstance(out, list) else []


async def add_channel_reaction(team_id: str, channel_id: str, message_id: str,
                               reaction: str) -> dict:
    """Set (replaces any existing) emoji reaction on a channel message."""
    return await get_manager().call(SERVER, "graph_add_channel_reaction", {
        "teamId": team_id, "channelId": channel_id, "messageId": message_id,
        "reactionType": reaction})


async def reply_email(email_id: str, comment: str, reply_all: bool = False) -> dict:
    return await get_manager().call(SERVER, "graph_reply_email", {
        "emailId": email_id, "comment": comment, "replyAll": reply_all,
    })


async def move_email(email_id: str, destination: str) -> dict:
    return await get_manager().call(SERVER, "graph_move_email", {
        "emailId": email_id, "destinationFolder": destination,
    })


def strip_html(html: str) -> str:
    text = re.sub(r"<br ?/?>|</p>", "\n", html or "")
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"&nbsp;", " ", text).strip()
