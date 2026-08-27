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

NOISE_SENDERS = (
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "notifications@github.com", "github.com",
    "digest", "newsletter", "unsubscribe",
    "engage.mail.microsoft", "azure.microsoft.com", "azuredevops@microsoft.com",
    "notifications@atlassian.net", "jira@", "confluence@",
    "dependabot", "renovateapp", "automate@", "automated@",
    "alerts@", "monitoring@", "pingdom", "pagerduty",
    "postmaster@", "mailer-daemon",
    "test@your-org.example",  # internal automated reminder/test sender — add your org's here
)

NOISE_SUBJECT_PREFIXES = (
    "build #", "build failed", "build passed", "build succeeded",
    "pipeline ", "[jenkins]", "[ci]", "ci build",
    "deployment ", "deploy to ", "deployed to ",
    "[dependabot]", "bump ", "chore(deps)",
    "daily digest", "weekly digest", "weekly report",
    "automated notification", "automatic notification",
    "invitation to subscribe", "you've been added",
    "status update:", "system alert:", "health check",
    "[jira]", "[confluence]", "[azure devops]",
    "re: automated", "fwd: automated",
    "notification:", "alert:", "action required: subscription",
    "your azure subscription", "azure active directory",
    "azure devops personal access token", "personal access token nearing",
    "canceled:", "cancelled:",  # calendar cancellation notifications
)

NOISE_BODY_PATTERNS = (
    "your meeting was forwarded",
    "has forwarded your meeting request",
    "has accepted your meeting",
    "has declined your meeting",
    "has tentatively accepted your meeting",
    "you don't often get email from",  # MS external sender warning on calendar syncs
)

# Group broadcast openers in email bodies: these are mass/team emails, not 1:1 asks.
EMAIL_GROUP_OPENERS = (
    "hey team,", "hi team,", "hello team,", "hey team\n", "hi team\n",
    "hey all,", "hi all,", "hello all,", "hey all\n", "hi all\n",
    "hey everyone,", "hi everyone,", "hello everyone,", "hey everyone\n",
    "hi folks,", "hey folks,", "hello folks,",
    "dear team,", "dear all,", "all,\n", "team,\n",
    "good morning team", "good afternoon team", "good morning all", "good afternoon all",
)

NOISE_SUBJECT_KEYWORDS = (
    "unsubscribe", "mailing list", "listserv",
    "out of office", "automatic reply", "auto-reply",
)

EMAIL_PROMPT = """Classify each email for the user (an engineering lead). Triage to tier, then draft a reply only when one is needed.

TIERS:
- A: pure FYI, automated notification, marketing, status email with no open question, or VIP announcement that doesn't ask the user anything. needs_reply=false, proposed_reply="".
- B: substantive question or coordination ask from a real person. Draft the real reply. Direct, first person, no corporate filler. When the user disagrees or the ask is unreasonable, say so plainly with a reason — don't soften it. No em dashes.
- C: VIP sender asking the user something SPECIFICALLY AND DIRECTLY, or emotional/negative tone, money/budget, HR, deadline at risk today, incident. Same as B but more measured; NEVER auto-sends.
  VIP GROUP EMAIL RULE: when a VIP sends to "All", "Everyone", a distribution list, or CC's multiple people without naming the user specifically in the body — that is a GROUP BROADCAST, tier A (needs_reply=false). Tier C ONLY when the VIP addresses the user by name or the email is clearly 1:1.
  VIP NUANCE: a VIP mass email, announcement, FYI, or email not directed at the user is tier A (needs_reply=false). Tier C only when the VIP is directing a specific request at the user personally.

GROUP BROADCAST RULE (applies to ALL senders, not just VIPs): an email that opens with "Hey Team", "Hi All", "Hello Everyone", "Dear Team", etc. is a mass email — tier A (needs_reply=false), regardless of who sent it. The user doesn't need to personally reply to team-wide announcements.
ESCALATION: B vs C in doubt → pick C only if there is real stakes or the VIP is directly and specifically addressing the user.
needs_reply=false for notifications, mass announcements, auto-generated emails, mailing lists, group broadcasts, or anything the user wasn't directly named in as the specific recipient.
reasoning: one concrete auditable sentence naming the exact signal (e.g. "Jane Doe VIP asking the user by name for ETA on X").
proposed_reply: write the actual reply, not a template or placeholder. Under 150 words. Direct. State opinions plainly. If the user has a strong view, express it.

Emails (JSON):
{items}
"""


def _is_noise(sender_addr: str, subject: str, body: str = "") -> bool:
    s = (sender_addr or "").lower()
    if any(n in s for n in NOISE_SENDERS):
        return True
    sub = (subject or "").lower().strip()
    if any(sub.startswith(p) for p in NOISE_SUBJECT_PREFIXES):
        return True
    if any(kw in sub for kw in NOISE_SUBJECT_KEYWORDS):
        return True
    b = (body or "").lower()
    if any(p in b for p in NOISE_BODY_PATTERNS):
        return True
    # Group broadcast openers — team-wide emails the user wasn't specifically asked about.
    if any(b.startswith(p) for p in EMAIL_GROUP_OPENERS):
        return True
    return False


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

    with db.transaction():
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
            if _is_noise(addr.get("address", ""), e.get("subject", ""),
                         e.get("bodyPreview", "")):
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
    from ..triage import _NOISE_WORDS

    new_items = db.query(
        "SELECT * FROM items WHERE source='email' AND status='new' "
        "ORDER BY received_at ASC LIMIT 20")
    if not new_items:
        return 0

    # Fast-path: trivially short / noise-only email bodies skip the LLM.
    fast, real = [], []
    for it in new_items:
        body = (it["content"] or "").strip().lower()
        if (body in _NOISE_WORDS or len(body) <= 3) and not is_vip(it["sender"], it["sender_id"]):
            fast.append(it)
        else:
            real.append(it)

    n = 0
    if fast:
        fast_ids = [it["id"] for it in fast]
        ph = ",".join("?" * len(fast_ids))
        with db.transaction():
            # 1 batch UPDATE instead of N individual update_item() calls for noise emails
            db.execute(
                f"UPDATE items SET tier='A', urgent=0, status='classified', "
                f"tier_reasoning='noise (email fast path)', updated_at=? WHERE id IN ({ph})",
                (db.now(), *fast_ids))
            for it in fast:
                db.audit("classified", {"source": "email", "tier": "A",
                                        "reasoning": "fast path: trivial body",
                                        "sender": it["sender"], "subject": it["subject"],
                                        "needs_reply": False}, item_id=it["id"])
                log.debug("email fast-path: %s / %s", it["sender"], it["subject"])
        n = len(fast)

    if not real:
        return n

    payload = [{
        "item_id": it["id"], "from": it["sender"], "from_addr": it["sender_id"],
        "sender_is_vip": is_vip(it["sender"], None),
        "subject": it["subject"], "body_preview": (it["content"] or "")[:1200],
    } for it in real]
    try:
        result = await run_agent(
            CHIEF, EMAIL_PROMPT.replace("{items}", json.dumps(payload, ensure_ascii=False, indent=1)),
            schema=TRIAGE_SCHEMA, with_tools=False, max_turns=4)
    except Exception as e:
        log.exception("email classification failed")
        db.audit("triage_error", {"source": "email", "error": str(e)[:300]})
        return n
    by_id = {it["id"]: it for it in real}
    with db.transaction():
        for c in result["classifications"]:
            item = by_id.get(c["item_id"])
            if not item:
                continue
            # Escalate VIP from B→C, but respect A (FYI/broadcast) and C already.
            llm_tier = c["tier"]
            if is_vip(item["sender"], None) and llm_tier == "B":
                tier = "C"
            else:
                tier = llm_tier
            db.update_item(item["id"], tier=tier, tier_reasoning=c["reasoning"],
                           urgent=int(c["urgent"]), status="classified")
            db.audit("classified", {"source": "email", "tier": tier, "reasoning": c["reasoning"],
                                    "sender": item["sender"], "subject": item["subject"],
                                    "needs_reply": c["needs_reply"]}, item_id=item["id"])
            if c["needs_reply"] and c["proposed_reply"]:
                conv_id = item.get("conversation_id") or item["external_id"]
                # Per-sender cap: don't pile up drafts for the same sender.
                max_per_sender = int(policy().get("triage", {}).get("max_drafts_per_sender", 2))
                sender_pending = db.query(
                    "SELECT COUNT(*) AS n FROM drafts d JOIN items i ON i.id=d.item_id "
                    "WHERE d.status='pending' AND d.channel='email' AND i.sender=?",
                    (item["sender"],))[0]["n"]
                if sender_pending >= max_per_sender and not c.get("urgent"):
                    db.update_item(item["id"], action_taken=f"email draft skipped: {item['sender']} already has {sender_pending} pending email drafts (cap={max_per_sender})")
                    n += 1
                    continue
                draft_id = db.execute(
                    "INSERT INTO drafts(item_id, channel, recipient, reply_to_ref, body, created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (item["id"], "email", item["sender"], item["external_id"],
                     c["proposed_reply"], db.now()))
                db.update_item(item["id"], status="drafted", action_taken="email draft awaiting approval")
                db.audit("draft_created", {"draft_id": draft_id, "channel": "email",
                                           "body": c["proposed_reply"]}, item_id=item["id"])
                # Supersede older pending drafts in this email thread.
                db.execute(
                    "UPDATE drafts SET status='superseded', resolved_at=? "
                    "WHERE status='pending' AND channel='email' AND id != ? "
                    "AND item_id IN (SELECT id FROM items WHERE conversation_id=? AND source='email')",
                    (db.now(), draft_id, conv_id))
                vault.chieff_trace("Email", f"drafted reply to {item['sender']}: "
                                            f"{(item['subject'] or '')[:80]} (awaiting approval)")
            n += 1
    return n
