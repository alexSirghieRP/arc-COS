"""Teams sweep: pull new chat messages into items.

Uses the chat list's lastMessagePreview as a cheap change detector, then reads
full messages only for chats with activity past the watermark.
"""

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


async def sweep() -> dict:
    watermark_s = db.get_setting("teams_watermark")
    watermark = (
        _parse_ts(watermark_s) if watermark_s
        else datetime.now(timezone.utc) - timedelta(hours=FIRST_RUN_LOOKBACK_HOURS)
    )
    newest = watermark
    new_items = 0

    chats = await graph.list_chats(top=50)
    for chat in chats:
        preview = chat.get("lastMessagePreview") or {}
        created = preview.get("createdDateTime")
        if not created or _parse_ts(created) <= watermark:
            continue

        chat_id = chat["id"]
        topic = chat.get("topic") or chat.get("chatType", "chat")
        messages = await graph.read_chat_messages(chat_id, top=20)
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
            cfg = policy()
            chieff_cfg = cfg.get("chieff", {})
            triggers = chieff_cfg.get("triggers") or [chieff_cfg.get("trigger", "@chieff")]
            own_sigs = [cfg["messaging"].get("assistant_attribution", ""),
                        chieff_cfg.get("attribution", "~ CoS")]
            body_l = body.lower()
            is_chieff = any(t.lower() in body_l for t in triggers) and not any(
                s and s in body for s in own_sigs)  # not CoS's own replies
            if from_user.get("id") == _me_id() and not is_chieff:
                continue  # the user's messages only matter when he addresses Chieff
            mentioned = any(
                (m.get("mentioned", {}).get("user") or {}).get("id") == _me_id()
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

    db.set_setting("teams_watermark", newest.isoformat())
    log.info("teams sweep: %d new items, watermark %s", new_items, newest.isoformat())

    from ..triage import triage_new_items
    from .chieff import handle_mentions
    chieff = await handle_mentions()
    triage = await triage_new_items()
    return {"new_items": new_items, "watermark": newest.isoformat(),
            "chieff": chieff, "triage": triage}
