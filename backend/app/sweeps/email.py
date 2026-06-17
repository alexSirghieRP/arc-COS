"""Email sweep. HARD RULE: email is draft-only; nothing outbound is ever
auto-sent here, regardless of tier or away state. Obvious noise is dismissed
on the board (not moved or deleted in the mailbox in v1)."""

import json
import logging
from datetime import datetime, timedelta, timezone

from .. import db, graph, vault
from ..agents import CHIEF, run_agent
from ..config import policy
from ..triage import TRIAGE_SCHEMA, is_vip

log = logging.getLogger("chief.sweep.email")

NOISE_SENDERS = ("noreply", "no-reply", "notifications@github.com", "digest",
                 "newsletter", "donotreply", "engage.mail.microsoft")

EMAIL_PROMPT = """Classify each email for the user using tiers A (trivial/FYI), B (routine but substantive, draft a reply), C (VIP, emotional, money, HR, personnel, deadline at risk, incident).
Email is NEVER auto-answered; proposed_reply is always just a draft for the user.
For pure notifications or FYIs set needs_reply=false and proposed_reply="".
reasoning: one auditable sentence. Replies: brief, plain, no em dashes.

Emails (JSON):
{items}
"""


def _is_noise(sender_addr: str, subject: str) -> bool:
    s = (sender_addr or "").lower()
    return any(n in s for n in NOISE_SENDERS)


async def sweep() -> dict:
    watermark_s = db.get_setting("email_watermark")
    first_run = watermark_s is None
    watermark = (
        datetime.fromisoformat(watermark_s) if watermark_s
        else datetime.now(timezone.utc) - timedelta(days=3)
    )
    newest = watermark

    emails = await graph.list_emails(top=50, time_filter="thisweek" if first_run else None)
    me = policy()["me"]["email"].lower()
    new_items, noise = 0, 0

    for e in emails:
        received_s = e.get("receivedDateTime")
        if not received_s:
            continue
        received = datetime.fromisoformat(received_s.replace("Z", "+00:00"))
        if received <= watermark:
            continue
        if received > newest:
            newest = received
        addr = ((e.get("from") or {}).get("emailAddress") or {})
        if (addr.get("address") or "").lower() == me:
            continue
        item_id, is_new = db.upsert_item(
            source="email", type_="email", external_id=e["id"],
            conversation_id=e.get("conversationId") or e["id"],
            sender=addr.get("name"), sender_id=addr.get("address"),
            subject=e.get("subject"), content=(e.get("bodyPreview") or "")[:2000],
            received_at=received_s,
        )
        if not is_new:
            continue
        new_items += 1
        if _is_noise(addr.get("address", ""), e.get("subject", "")):
            db.update_item(item_id, status="dismissed", action_taken="noise (board only)")
            noise += 1
        else:
            db.audit("item_ingested", {"source": "email", "sender": addr.get("name"),
                                       "subject": e.get("subject")}, item_id=item_id)

    db.set_setting("email_watermark", newest.isoformat())

    classified = await _classify_new_emails()
    log.info("email sweep: %d new (%d noise), %d classified", new_items, noise, classified)
    return {"new_items": new_items, "noise": noise, "classified": classified}


async def _classify_new_emails() -> int:
    new_items = db.query(
        "SELECT * FROM items WHERE source='email' AND status='new' "
        "ORDER BY received_at ASC LIMIT 10")
    if not new_items:
        return 0
    payload = [{
        "item_id": it["id"], "from": it["sender"], "from_addr": it["sender_id"],
        "sender_is_vip": is_vip(it["sender"], None),
        "subject": it["subject"], "body_preview": (it["content"] or "")[:1200],
    } for it in new_items]
    try:
        result = await run_agent(
            CHIEF, EMAIL_PROMPT.replace("{items}", json.dumps(payload, ensure_ascii=False, indent=1)),
            schema=TRIAGE_SCHEMA, with_tools=False, max_turns=4)
    except Exception as e:
        log.exception("email classification failed")
        db.audit("triage_error", {"source": "email", "error": str(e)[:300]})
        return 0
    by_id = {it["id"]: it for it in new_items}
    n = 0
    for c in result["classifications"]:
        item = by_id.get(c["item_id"])
        if not item:
            continue
        tier = "C" if is_vip(item["sender"], None) else c["tier"]
        db.update_item(item["id"], tier=tier, tier_reasoning=c["reasoning"],
                       urgent=int(c["urgent"]), status="classified")
        db.audit("classified", {"source": "email", "tier": tier, "reasoning": c["reasoning"],
                                "sender": item["sender"], "subject": item["subject"],
                                "needs_reply": c["needs_reply"]}, item_id=item["id"])
        if c["needs_reply"] and c["proposed_reply"]:
            draft_id = db.execute(
                "INSERT INTO drafts(item_id, channel, recipient, reply_to_ref, body, created_at) "
                "VALUES(?,?,?,?,?,?)",
                (item["id"], "email", item["sender"], item["external_id"],
                 c["proposed_reply"], db.now()))
            db.update_item(item["id"], status="drafted", action_taken="email draft awaiting approval")
            db.audit("draft_created", {"draft_id": draft_id, "channel": "email",
                                       "body": c["proposed_reply"]}, item_id=item["id"])
            vault.chieff_trace("Email", f"drafted reply to {item['sender']}: "
                                        f"{(item['subject'] or '')[:80]} (awaiting approval)")
        n += 1
    return n
