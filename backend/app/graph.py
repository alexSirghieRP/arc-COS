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
        "selectFields": "id,subject,start,end,location,organizer,isAllDay,showAs,webLink",
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


async def chats_cached(ttl: int = 300) -> list[dict]:
    global _chats_cache, _chats_ts
    import time
    if time.time() - _chats_ts > ttl:
        _chats_cache = await list_chats(top=50)
        _chats_ts = time.time()
    return _chats_cache


async def read_chat_messages(chat_id: str, top: int = 20) -> list[dict]:
    out = await get_manager().call(SERVER, "graph_read_chat_messages", {"chatId": chat_id, "top": top})
    if isinstance(out, dict):
        return out.get("messages", out.get("value", []))
    return out if isinstance(out, list) else []


import html as _html

_HTML_TAG = re.compile(r"</?(p|ul|ol|li|br|a|b|strong|em|i|h[1-6])\b", re.I)
_URL = re.compile(r"(https?://[^\s<]+)")


def to_html(body: str) -> str:
    """Teams renders chat/channel messages as HTML, so plain text with newlines
    collapses into a wall of text and bare URLs don't link. Convert plain text
    to paragraphs/<br> with auto-linked URLs; leave already-HTML bodies alone."""
    if not body or _HTML_TAG.search(body):
        return body
    esc = _html.escape(body)
    esc = _URL.sub(r'<a href="\1">\1</a>', esc)
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
    email returns his Teams self-chat (idempotent)."""
    out = await get_manager().call(SERVER, "graph_create_chat_with_user", {"userEmail": email})
    return out if isinstance(out, dict) else {}


async def search_users(name: str) -> list[dict]:
    out = await get_manager().call(SERVER, "graph_search_users", {"displayName": name})
    return out if isinstance(out, list) else out.get("value", []) if isinstance(out, dict) else []


async def post_channel_message(team_id: str, channel_id: str, message: str) -> dict:
    return await get_manager().call(SERVER, "graph_post_channel_message", {
        "teamId": team_id, "channelId": channel_id, "message": to_html(message),
    })


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
