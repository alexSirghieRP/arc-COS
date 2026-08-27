"""Teams sweep: pull new chat messages into items.

Uses the chat list's lastMessagePreview as a cheap change detector, then reads
full messages only for chats with activity past the watermark. Chat-message
reads are issued concurrently (one asyncio task per active chat) to eliminate
the sequential wait across many chats.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from .. import db, graph
from ..config import policy

log = logging.getLogger("chief.sweep.teams")

FIRST_RUN_LOOKBACK_HOURS = 24


def _me_id() -> str:
    return policy()["me"]["aad_id"]


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _process_messages(messages, chat_id, topic, watermark, me_id, cfg) -> tuple[int, datetime]:
    """Ingest new messages from one chat. Returns (new_item_count, newest_ts)."""
    newest = watermark
    new_items = 0
    chieff_cfg = cfg.get("chieff", {})
    triggers = chieff_cfg.get("triggers") or [chieff_cfg.get("trigger", "@chieff")]
    own_sigs = [cfg["messaging"].get("assistant_attribution", ""),
                chieff_cfg.get("attribution", "~ CoS")]
    for msg in messages:
        ts_s = msg.get("createdDateTime")
        if not ts_s:
            continue
        ts = _parse_ts(ts_s)
        if ts <= watermark:
            continue
        if ts > newest:
            newest = ts
        from_user = (msg.get("from") or {}).get("user") or {}
        if not from_user:
            continue
        if msg.get("messageType") not in (None, "message"):
            continue
        body = graph.strip_html((msg.get("body") or {}).get("content", ""))
        if not body.strip():
            continue
        body_l = body.lower()
        is_chieff = any(t.lower() in body_l for t in triggers) and not any(
            s and s in body for s in own_sigs)
        if from_user.get("id") == me_id and not is_chieff:
            continue
        mentioned = any(
            (m.get("mentioned", {}).get("user") or {}).get("id") == me_id
            for m in msg.get("mentions", [])
        )
        item_id, is_new = db.upsert_item(
            source="teams_chat",
            type_="chieff" if is_chieff else ("mention" if mentioned else "ping"),
            external_id=f"{chat_id}:{msg.get('id')}",
            conversation_id=chat_id,
            sender=from_user.get("displayName"),
            sender_id=from_user.get("id"),
            subject=topic,
            content=body[:4000],
            received_at=ts_s,
        )
        if is_new:
            new_items += 1
            db.audit("item_ingested", {
                "source": "teams_chat", "chat": topic,
                "sender": from_user.get("displayName"), "preview": body[:200],
            }, item_id=item_id)
    return new_items, newest


async def sweep() -> dict:
    watermark_s = db.get_setting("teams_watermark")
    watermark = (
        _parse_ts(watermark_s) if watermark_s
        else datetime.now(timezone.utc) - timedelta(hours=FIRST_RUN_LOOKBACK_HOURS)
    )

    me_id = _me_id()
    cfg = policy()

    chats = await graph.chats_cached()

    # Filter to chats with activity since the watermark.
    active_chats = []
    for chat in chats:
        preview = chat.get("lastMessagePreview") or {}
        created = preview.get("createdDateTime")
        if not created:
            continue
        created_ts = _parse_ts(created)
        if created_ts <= watermark:
            continue
        mins_since = (datetime.now(timezone.utc) - created_ts).total_seconds() / 60
        top = 30 if mins_since < 30 else 20
        active_chats.append((chat, top))

    if not active_chats:
        db.set_setting("teams_watermark", watermark.isoformat())
        log.info("teams sweep: no new activity")
        from ..triage import triage_new_items
        from .chieff import handle_mentions
        chieff, triage = await asyncio.gather(handle_mentions(), triage_new_items())
        return {"new_items": 0, "watermark": watermark.isoformat(),
                "chieff": chieff, "triage": triage}

    # Fetch messages for all active chats concurrently.
    async def _fetch_chat(chat, top):
        chat_id = chat["id"]
        topic = chat.get("topic") or chat.get("chatType", "chat")
        try:
            messages = await graph.read_chat_messages(chat_id, top=top)
            return chat_id, topic, messages
        except Exception as e:
            log.warning("teams sweep: failed to read chat %s: %s", chat_id[:20], e)
            return chat_id, topic, []

    fetched = await asyncio.gather(*[_fetch_chat(c, t) for c, t in active_chats])

    newest = watermark
    new_items = 0
    with db.transaction():
        for chat_id, topic, messages in fetched:
            n, ts = _process_messages(messages, chat_id, topic, watermark, me_id, cfg)
            new_items += n
            if ts > newest:
                newest = ts
        db.set_setting("teams_watermark", newest.isoformat())
    log.info("teams sweep: %d new items across %d chats, watermark %s",
             new_items, len(active_chats), newest.isoformat())

    from ..triage import triage_new_items
    from .chieff import handle_mentions
    # Parallel: chieff mention handling and triage are independent (was sequential)
    chieff, triage = await asyncio.gather(handle_mentions(), triage_new_items())
    return {"new_items": new_items, "watermark": newest.isoformat(),
            "chieff": chieff, "triage": triage}
