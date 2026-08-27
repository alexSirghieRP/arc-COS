"""Ping triage: rules + LLM classification, then guarded actions per tier."""

import asyncio
import json
import logging
from datetime import date, datetime, timedelta, timezone

from . import actions, db, graph, state
from .agents import CHIEF, run_agent
from .config import policy

log = logging.getLogger("chief.triage")

MAX_AGE_HOURS = 8          # older pings get surfaced but not auto-handled
BATCH_SIZE = 6             # 6 items/batch; retry-on-timeout handles edge cases safely
MAX_BATCHES_PER_SWEEP = 5  # 5×6=30 per sweep cycle (+50% throughput vs previous 4×5=20)

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "classifications": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "integer"},
                    "tier": {"type": "string", "enum": ["A", "B", "C"]},
                    "reasoning": {"type": "string"},
                    "needs_reply": {"type": "boolean"},
                    "sender_is_waiting": {"type": "boolean"},
                    "urgent": {"type": "boolean"},
                    "proposed_reply": {"type": "string"},
                },
                "required": ["item_id", "tier", "reasoning", "needs_reply",
                             "sender_is_waiting", "urgent", "proposed_reply"],
            },
        }
    },
    "required": ["classifications"],
}

TRIAGE_PROMPT = """Classify each incoming Teams message for the user (an engineering lead).

TIERS:
- A (trivial / self-service): status questions with obvious answers, doc/link shares, quick scheduling confirms, FYIs with no ask, thank-you messages, emoji. proposed_reply = the complete answer in 1-2 sentences, or a brief acknowledgement if nothing is needed.
  Examples: "is PR #660 merged?" → "Yes, merged last night." | "can you share the sprint doc?" → "Here it is: [link]. Let me know if you need anything else." | "Thanks!" → "Of course!"
- B (substantive work): real technical or project questions, decision requests, coordination, code/architecture questions, unblocking asks. proposed_reply = a full draft answer for the user to approve or tweak. Not a placeholder like "I'll look into it" — actually draft the answer from context.
  Examples: "can you review the Firestore index design?" → draft a real opinion | "should we use L1 or L2 for this?" → draft a concrete recommendation.
- C (high stakes / VIPs): a VIP is directly addressing the user specifically with a question, request, or decision — OR — emotional/negative tone (frustration, concern), money/budget/contracts, HR/personnel, deadline explicitly at risk TODAY, production incidents, anything where a wrong response is costly. proposed_reply = a careful, measured draft. NEVER auto-sends.
  VIP GROUP BROADCAST RULE: when a VIP addresses "Everyone", "team", "folks", "all", "@channel", or uses inclusive language without naming the user specifically — that is a GROUP BROADCAST, tier A (needs_reply=false). Examples: "Everyone can we connect", "Everyone please do X", "team FYI", "can we all connect". Tier C ONLY when the VIP names the user directly or the message is clearly a 1:1.
  VIP MULTI-NAME RULE: if a VIP message begins by listing 2+ people's names (e.g. "Jane Doe John Smith Sam Carter"), that is a GROUP BROADCAST to a named list — tier A (needs_reply=false). The fact that the user's name appears in a list does NOT make it a direct 1:1 message.
  VIP NUANCE: a VIP mass email, announcement, FYI, thank-you, emoji, or @mentioning someone else — tier A (needs_reply=false). Tier C only when the VIP is directing a specific request at the user alone.

ESCALATION RULE: when in doubt A vs B → pick B. When in doubt B vs C → pick C only if there is genuine stakes (VIP direct ask, risk, negativity). Group broadcasts from VIPs are tier A even if they could benefit from a response.
urgent=true: production incident, visibly angry stakeholder, or hard deadline WITHIN HOURS (not this week).
sender_is_waiting=true: sender asked the user a direct question (has "?"), or expressed clear time pressure.
needs_reply=false: pure FYI, bot notification, group chat chatter, group broadcast ("Everyone/team/folks"), or anything the user wasn't addressed by name in.
reasoning: one crisp sentence so the user can audit the tier call in 2 seconds. Name the specific signal.
Reply style: direct, confident, first person. The user states their view plainly and defends it with specific reasoning when challenged. They push back when they disagree — no diplomatic softening, no hedging with "it depends" when they have a clear opinion. Not warm-and-fuzzy; collegial but direct. No corporate filler. No em dashes. No "happy to help" or "great question". When they have a strong view, they say it straight: "No, that's the wrong call because X" or "I'd go with Y — here's why." Sound like someone who argues their position when it matters, not someone who accommodates to avoid friction.
For channel messages (source=teams_channel): needs_reply=false unless the user is directly @mentioned.

Messages (JSON):
{items}
"""


def _vip_ids() -> set[str]:
    return {v.get("aad_id") for v in policy().get("vip", []) if v.get("aad_id")}


def _vip_names() -> set[str]:
    return {v["name"].lower() for v in policy().get("vip", []) if v.get("name")}


def is_vip(sender: str | None, sender_id: str | None) -> bool:
    if sender_id and sender_id in _vip_ids():
        return True
    return bool(sender) and sender.lower() in _vip_names()


async def estimate_return_time() -> str:
    """A realistic 'the user will get back to you around X' string from calendar + work hours."""
    now = datetime.now().astimezone()
    wh = policy()["work_hours"]
    end_h, end_m = map(int, wh["end"].split(":"))
    start_h, start_m = map(int, wh["start"].split(":"))
    work_end = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)

    if not state.in_work_hours(now):
        nxt = now
        for _ in range(7):
            nxt += timedelta(days=1)
            probe = nxt.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
            if state.in_work_hours(probe):
                return probe.strftime("%A %H:%M") if probe.date() != now.date() else probe.strftime("%H:%M")
        return "10:00 tomorrow"

    try:
        events = await graph.calendar_events_cached(date.today())
    except Exception:
        events = []
    candidate = now + timedelta(minutes=30)
    for e in sorted(events, key=lambda e: e.get("start_local", "")):
        s, en = e.get("start_local"), e.get("end_local")
        if not s or not en:
            continue
        s_dt, e_dt = datetime.fromisoformat(s), datetime.fromisoformat(en)
        if s_dt <= candidate <= e_dt:
            candidate = e_dt + timedelta(minutes=15)
    if candidate > work_end:
        return "10:00 tomorrow"
    return candidate.strftime("%H:%M")


_NOISE_WORDS = frozenset([
    # acknowledgements
    "ok", "okay", "k", "kk", "ack", "acknowledged", "received", "roger",
    # thanks
    "thanks", "thank you", "ty", "thx", "tysm", "thank u", "many thanks",
    # agreements
    "got it", "noted", "sounds good", "makes sense", "understood", "copy",
    "will do", "on it", "on it!", "sure", "sure!", "absolutely",
    "no problem", "np", "nbd", "anytime",
    # confirmations
    "done", "done!", "all good", "all set", "good", "perfect", "great",
    "awesome", "excellent", "brilliant", "nice", "cool",
    # common workplace agreement phrases
    "works for me", "that works", "sounds great", "looks good", "looks great",
    "that's great", "that's good", "that's perfect", "great job", "well done",
    "good job", "good work", "nice work", "great work", "nicely done",
    "i'll take a look", "i'll check", "i'll look into it", "on my way",
    "heading over", "be right there", "be there in a sec", "one sec",
    "just a sec", "give me a sec", "brb", "omw", "ack",
    # y/n
    "yes", "yep", "yup", "yeah", "nope", "no", "nah",
    # code review
    "lgtm", "lgtm!", "+1", "approved", "merged",
    # reactions / emojis (standalone)
    "👍", "🙏", "✅", "👌", "🎉", "🔥", "💯", "🚀", "❤️", "😊", "😄", "😀",
    "👏", "🤝", "💪", "🙌", "⭐", "🌟", "✔️", "☑️",
    # calendar/meeting auto-replies
    "accepted", "declined", "tentative",
    # scheduling confirmations
    "see you then", "see you there", "see you soon", "see you tomorrow",
    "talk soon", "talk later", "chat later", "catch you later",
    # busy / OOO signals (pure status, no question embedded)
    "in a meeting", "on a call", "back in a bit",
    "stepping away", "stepping out", "heading to lunch",
    # number-only replies (answering "how many?" or slot confirmations)
    "1", "2", "3", "4", "5",
    # thanks + name combos stripped to core
    "thank you!", "thanks!", "thanks so much", "thank you so much",
    "appreciate it", "i appreciate it", "appreciate your help",
    "much appreciated",
])

# Patterns indicating noise even when body is longer (substring match on lowercased body).
_NOISE_PATTERNS = (
    "has accepted your meeting",
    "has declined your meeting",
    "has tentatively accepted",
    "accepted this invitation",
    "declined this invitation",
    "i'll join the meeting",
    "i will join the meeting",
    "joined the meeting",
    "left the meeting",
    "meeting ended",
    "call ended",
    # PR channel hand-offs (source=teams_channel, @me not mentioned)
    "up for review: pr #",
    "approved and green",
    "approved - the",
    "re-approved and green",
    "back to you",
    # Group chat reactions where the user is not the target
    "approved, thank you",
    "your meeting was forwarded",
    "has forwarded your meeting request",
    # Meeting/calendar system messages
    "changed the meeting details",
    "meeting has been updated",
    "you've been added to the meeting",
)

# Exact-match noise for short messages (strip punctuation first).
_NOISE_WORDS_EXTRA = frozenset([
    "on a call", "in a meeting now", "running late", "just joined",
    "heading out", "wrapping up", "one moment", "one sec", "almost done",
    "give me a moment", "on my way now", "be there shortly",
    "my bad", "my mistake", "sorry about that", "no worries",
    "not a problem", "all good!", "you got it", "got it!",
])


_GROUP_BROADCAST_OPENERS = (
    "everyone ", "everyone,", "everyone.", "everyone!", "everyone\n",
    "hi everyone", "hey everyone", "hello everyone", "hi team", "hey team",
    "hello team", "hi all", "hey all", "hello all", "hi folks", "hey folks",
    "hello folks", "team,", "team.", "team!", "folks,", "folks.",
    "@channel", "@here", "@all",
)


_MULTI_NAME_RE = None  # lazy-compiled regex for VIP multi-name list detection


def _is_multi_name_list(content: str) -> bool:
    """Detect 'Firstname Surname OtherName ...' group-list pattern at message start.
    Matches when 3+ consecutive Title-Cased words open the message (name list, not a sentence)."""
    import re
    global _MULTI_NAME_RE
    if _MULTI_NAME_RE is None:
        # 3+ title-case words with no sentence separator between them
        _MULTI_NAME_RE = re.compile(r'^([A-Z][a-z]+\s+){2,}[A-Z][a-z]+')
    # Check first 120 chars of original content (before any sentence punctuation)
    head = content.strip()[:120]
    first_sentence_end = min(
        (head.find(c) for c in '.!?\n' if head.find(c) != -1),
        default=len(head)
    )
    return bool(_MULTI_NAME_RE.match(head[:first_sentence_end]))


def _vip_fast_path(item: dict) -> dict | None:
    """Fast-path for VIP items that are unambiguously group broadcasts.
    VIPs skip the main _fast_path() so this handles their obvious noise separately.
    Intercepts: clear group-broadcast openers AND multi-name list headers.
    Only sends to LLM when the user is the clear sole addressee."""
    if not is_vip(item["sender"], item["sender_id"]):
        return None
    if item["type"] == "mention":
        return None  # always LLM when the user is explicitly mentioned
    original = (item["content"] or "").strip()
    body = original.lower()
    # Multi-name list: "Jane Doe John Smith Sam Carter ..." → group broadcast
    if _is_multi_name_list(original):
        return {
            "item_id": item["id"], "tier": "A", "urgent": False,
            "needs_reply": False, "sender_is_waiting": False, "proposed_reply": "",
            "reasoning": "VIP multi-name group list (fast path)",
            "fast_path": True,
        }
    # VIP noise: pure acknowledgements (same words as non-VIP noise) — saves LLM for obvious cases
    stripped = body.rstrip("!. ")
    if (body in _NOISE_WORDS or body in _NOISE_WORDS_EXTRA
            or stripped in _NOISE_WORDS or stripped in _NOISE_WORDS_EXTRA
            or (len(body) <= 4 and not body.isalpha())
            or any(p in body for p in _NOISE_PATTERNS)):
        return {
            "item_id": item["id"], "tier": "A", "urgent": False,
            "needs_reply": False, "sender_is_waiting": False, "proposed_reply": "",
            "reasoning": "VIP noise/acknowledgement (fast path)",
            "fast_path": True,
        }
    # Reject if the user is named directly (but not as part of a list) — could be a 1:1
    me_first = (policy().get("me", {}).get("name") or "").split(" ")[0].lower()
    if me_first and me_first in body[:200]:
        return None
    if any(body.startswith(p) for p in _GROUP_BROADCAST_OPENERS):
        return {
            "item_id": item["id"], "tier": "A", "urgent": False,
            "needs_reply": False, "sender_is_waiting": False, "proposed_reply": "",
            "reasoning": "VIP group broadcast (fast path)",
            "fast_path": True,
        }
    return None


def _fast_path(item: dict) -> dict | None:
    """Rule-based pre-classifier for obvious noise. Returns a pre-filled
    classification dict or None when the LLM must decide."""
    body = (item["content"] or "").strip().lower()
    if item["type"] == "mention" or is_vip(item["sender"], item["sender_id"]):
        return None

    # Channel messages that don't @mention the user are always tier-A by policy.
    if item.get("source") == "teams_channel":
        return {
            "item_id": item["id"], "tier": "A", "urgent": False,
            "needs_reply": False, "sender_is_waiting": False, "proposed_reply": "",
            "reasoning": "channel message, owner not @mentioned (fast path)",
            "fast_path": True,
        }

    stripped = body.rstrip("!. ")
    is_noise = (
        body in _NOISE_WORDS
        or body in _NOISE_WORDS_EXTRA
        or (len(body) <= 3 and not body.isalpha())
        or stripped in _NOISE_WORDS
        or stripped in _NOISE_WORDS_EXTRA
        or any(p in body for p in _NOISE_PATTERNS)
        or any(body.startswith(p) for p in _GROUP_BROADCAST_OPENERS)
    )
    if is_noise:
        return {
            "item_id": item["id"], "tier": "A", "urgent": False,
            "needs_reply": False, "sender_is_waiting": False, "proposed_reply": "",
            "reasoning": "noise/acknowledgement/group-broadcast, no reply needed (fast path)",
        }
    return None


async def classify_batch(items: list[dict]) -> list[dict]:
    fast, slow = [], []
    fast_results = []
    for it in items:
        fp = _vip_fast_path(it) or _fast_path(it)
        if fp:
            fast_results.append(fp)
            fast.append(it["id"])
        else:
            slow.append(it)

    if fast:
        log.debug("fast-path classified %d items (no LLM)", len(fast))
        # Mark as fast-path so act_on_classification can record it in the audit entry.
        for fp_result in fast_results:
            fp_result["fast_path"] = True

    if not slow:
        return fast_results

    # Collect rejection feedback (last 8 rejected drafts with reasons).
    rejection_examples = db.query(
        "SELECT i.sender, substr(i.content,1,120) AS msg, d.reject_reason "
        "FROM drafts d JOIN items i ON i.id = d.item_id "
        "WHERE d.status='rejected' AND d.reject_reason IS NOT NULL AND d.reject_reason != '' "
        "ORDER BY d.resolved_at DESC LIMIT 8")
    # Positive calibration: last 3 approved drafts give the LLM style reference
    # for tone and length that the user actually sent without changes.
    approved_examples = db.query(
        "SELECT i.sender, substr(i.content,1,120) AS msg, substr(d.body,1,150) AS draft "
        "FROM drafts d JOIN items i ON i.id = d.item_id "
        "WHERE d.status='approved_sent' "
        "ORDER BY d.resolved_at DESC LIMIT 3")

    # Batch-fetch the last 2 interactions per unique sender in one query.
    slow_senders = list({it["sender"] for it in slow if it["sender"]})
    slow_ids = [it["id"] for it in slow]
    prior_by_sender: dict[str, list] = {s: [] for s in slow_senders}
    if slow_senders:
        placeholders_s = ",".join("?" * len(slow_senders))
        placeholders_id = ",".join("?" * len(slow_ids))
        # ROW_NUMBER per sender, ordered by recency; keep the 2 most recent per sender.
        prior_rows = db.query(
            f"SELECT sender, type, status, substr(COALESCE(content,''),1,200) AS content, "
            f"       ROW_NUMBER() OVER (PARTITION BY sender ORDER BY received_at DESC) AS rn "
            f"FROM items "
            f"WHERE sender IN ({placeholders_s}) AND id NOT IN ({placeholders_id})",
            tuple(slow_senders + slow_ids))
        for r in prior_rows:
            if r["rn"] <= 2:
                prior_by_sender.setdefault(r["sender"], []).append(
                    {"status": r["status"], "excerpt": r["content"]})

    payload = []
    for it in slow:
        entry = {
            "item_id": it["id"],
            "chat": it["subject"],
            "source": it.get("source", ""),
            "sender": it["sender"],
            "sender_is_vip": is_vip(it["sender"], it["sender_id"]),
            "mentioned_alex": it["type"] == "mention",
            "message": (it["content"] or "")[:1500],
        }
        if "queued for redraft" in (it.get("action_taken") or ""):
            entry["overdue_note"] = "A prior draft for this message expired unapproved — the reply is now 3+ days late. Open with a brief acknowledgement of the delay before addressing the substance."
        prior = prior_by_sender.get(it["sender"] or "", [])
        if prior:
            entry["recent_from_sender"] = prior
        payload.append(entry)

    # Include today's calendar so the LLM can propose real available slots
    # for scheduling requests (one API call shared across all items in the batch).
    calendar_ctx = ""
    try:
        from datetime import date as _date
        today_events = await graph.calendar_events_cached(_date.today())
        if today_events:
            slots = []
            for e in today_events:
                start = (e.get("start_local") or "")[:16]
                end = (e.get("end_local") or "")[:16]
                subj = (e.get("subject") or "")[:40]
                if start:
                    slots.append(f"{start}-{end[11:]} {subj}")
            if slots:
                calendar_ctx = "\n\nThe user's calendar today (for scheduling replies): " + "; ".join(slots)
    except Exception:
        pass

    prompt = TRIAGE_PROMPT.replace("{items}", json.dumps(payload, ensure_ascii=False, indent=1))
    if calendar_ctx:
        prompt = prompt.rstrip() + calendar_ctx + "\n"
    if approved_examples:
        approve_block = "\n".join(
            f"- To {r['sender']}: \"{(r['draft'] or '')[:120]}\""
            for r in approved_examples
        )
        prompt = prompt.rstrip() + (
            f"\n\nAPPROVED DRAFT STYLE (the user sent these without changes — match this tone and length):\n"
            f"{approve_block}\n"
        )
    if rejection_examples:
        reject_block = "\n".join(
            f"- {r['sender']}: \"{(r['msg'] or '')[:80]}\" → the user said: \"{r['reject_reason']}\""
            for r in rejection_examples
        )
        prompt = prompt.rstrip() + (
            f"\n\nRECENT REJECTION CALIBRATION (what the user said was off about prior drafts — "
            f"avoid these patterns in proposed_reply):\n{reject_block}\n"
        )

    result = await run_agent(
        CHIEF, prompt,
        schema=TRIAGE_SCHEMA, with_tools=False, max_turns=4, label="triage",
    )
    return fast_results + result["classifications"]


async def _auto_engage_allowed(item: dict, waiting: bool, needs_reply: bool) -> bool:
    """Unprompted sends only when the user was explicitly pinged, and recently.
    Group/meeting chats: a real @mention. 1:1s: a fresh message that clearly
    expects a reply. Drafts and board surfacing are unaffected by this gate."""
    t = policy()["triage"]
    if not t.get("reply_only_when_pinged", True):
        return True
    max_age = int(t.get("auto_reply_max_age_minutes", 60))
    try:
        received = datetime.fromisoformat((item["received_at"] or "").replace("Z", "+00:00"))
        if received.tzinfo is None:
            received = received.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    if datetime.now(timezone.utc) - received > timedelta(minutes=max_age):
        return False
    chats = {ch["id"]: ch for ch in await graph.chats_cached()}
    if chats.get(item["conversation_id"], {}).get("chatType") == "oneOnOne":
        return waiting and needs_reply
    return item["type"] == "mention"


async def act_on_classification(item: dict, c: dict, away: bool, pending_count: int | None = None):
    """Apply tier policy. VIP escalates B→C but respects A (FYI/broadcast)."""
    tier = c["tier"]
    if is_vip(item["sender"], item["sender_id"]) and tier == "B":
        tier = "C"
    urgent = bool(c["urgent"])
    db.update_item(item["id"], tier=tier, tier_reasoning=c["reasoning"],
                   urgent=int(urgent), status="classified")
    audit_detail = {
        "tier": tier, "reasoning": c["reasoning"], "sender": item["sender"],
        "needs_reply": c["needs_reply"], "waiting": c["sender_is_waiting"],
        "urgent": urgent, "conversation": item["subject"],
    }
    if c.get("fast_path"):
        audit_detail["fast_path"] = True
    db.audit("classified", audit_detail, item_id=item["id"])

    if not c["needs_reply"]:
        return
    chat_id = item["conversation_id"]
    autonomy = bool(policy().get("chieff", {}).get("autonomy")) and tier != "C"
    engage = await _auto_engage_allowed(item, c["sender_is_waiting"], c["needs_reply"])

    if tier == "A":
        if not engage:
            db.update_item(item["id"], action_taken="no explicit ping, surfaced only")
        elif autonomy and away:
            from .sweeps.chieff import autonomous_reply
            try:
                r = await autonomous_reply(item, tier, away)
                db.update_item(item["id"], status="auto_replied",
                               action_taken=f"chieff auto-replied ({r})")
            except actions.SendBlocked as e:
                db.update_item(item["id"], action_taken=f"chieff auto-reply blocked: {e}")
        elif away and c["proposed_reply"]:
            try:
                r = await actions.auto_send_teams(
                    chat_id, c["proposed_reply"], kind="auto_reply", item_id=item["id"],
                    sender=item["sender"])
                db.update_item(item["id"], status="auto_replied",
                               action_taken=f"auto-replied ({r})")
            except actions.SendBlocked as e:
                db.update_item(item["id"], action_taken=f"auto-reply blocked: {e}")
        return

    # Tier B and C both get a draft for the approval queue — unless the backlog
    # is already large, in which case only tier C (VIP/urgent) drafts are created.
    # Tier B items get surfaced without a draft when the queue is saturated;
    # The user sees them in Pings and can trigger a draft manually.
    triage_cfg = policy().get("triage", {})
    DRAFT_BACKLOG_LIMIT = int(triage_cfg.get("draft_backlog_limit", 25))
    # Per-sender limit: if a sender already has this many pending drafts, skip creating
    # another one until the existing ones are reviewed. Keeps the queue drainable.
    # urgent=true always overrides this (production incident, hard deadline today).
    MAX_DRAFTS_PER_SENDER = int(triage_cfg.get("max_drafts_per_sender", 2))
    if pending_count is None:
        pending_count = db.query("SELECT COUNT(*) AS n FROM drafts WHERE status='pending'")[0]["n"]
    backlog_saturated = pending_count >= DRAFT_BACKLOG_LIMIT

    sender_pending = 0
    if c["proposed_reply"] and item["sender"] and not urgent:
        sender_pending = db.query(
            "SELECT COUNT(*) AS n FROM drafts d JOIN items i ON i.id=d.item_id "
            "WHERE d.status='pending' AND d.channel='teams' AND i.sender=?",
            (item["sender"],))[0]["n"]
    sender_capped = sender_pending >= MAX_DRAFTS_PER_SENDER and not urgent

    # Smart draft gate: tier-C but sender not actively waiting AND not urgent → surface in
    # Pings only (saves approval-queue slots for items where a prompt response matters).
    # sender_is_waiting=true means the sender asked a direct question or flagged time pressure.
    waiting_or_urgent = c["sender_is_waiting"] or urgent
    needs_draft_quality = tier != "C" or waiting_or_urgent

    create_draft = (c["proposed_reply"]
                    and (tier == "C" or not backlog_saturated)
                    and not sender_capped
                    and needs_draft_quality)
    if create_draft:
        draft_id = db.execute(
            "INSERT INTO drafts(item_id, channel, recipient, reply_to_ref, body, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (item["id"], "teams", item["sender"], chat_id, c["proposed_reply"], db.now()))
        db.update_item(item["id"], status="drafted", action_taken="draft awaiting approval")
        db.audit("draft_created", {"draft_id": draft_id, "body": c["proposed_reply"],
                                   "conversation_id": chat_id}, item_id=item["id"])
        # Supersede any older pending drafts for this conversation — the thread has moved on.
        db.execute(
            "UPDATE drafts SET status='superseded', resolved_at=? "
            "WHERE status='pending' AND reply_to_ref=? AND id != ?",
            (db.now(), chat_id, draft_id))
    elif sender_capped:
        db.update_item(item["id"], action_taken=f"draft skipped: {item['sender']} already has {sender_pending} pending drafts (cap={MAX_DRAFTS_PER_SENDER})")
        log.info("draft skipped for %s (sender cap: %d pending)", item["sender"], sender_pending)
    elif backlog_saturated and tier == "B":
        db.update_item(item["id"], action_taken=f"draft skipped: backlog saturated ({pending_count} pending)")
        log.info("draft skipped for %s (backlog %d >= %d)", item["sender"], pending_count, DRAFT_BACKLOG_LIMIT)

    if tier == "B" and autonomy and c["sender_is_waiting"] and engage and away:
        from .sweeps.chieff import autonomous_reply
        try:
            await autonomous_reply(item, tier, away)
        except actions.SendBlocked:
            pass  # ack is best-effort; the draft is the real handling

    if tier == "C":
        if away and engage:
            await _tier_c_away_reply(item)
        if urgent and policy()["triage"].get("notify_on_urgent", True):
            actions.notify_mac(f"Urgent: {item['sender']}", (item["content"] or "")[:200])


async def _tier_c_away_reply(item: dict):
    """VIP 1:1s: professional Chieff concierge instead of the bare holding
    template, when enabled. Falls back to holding if blocked."""
    cfg = policy().get("chieff", {})
    if cfg.get("autonomy") and cfg.get("vip_concierge"):
        chats = {ch["id"]: ch for ch in await graph.chats_cached()}
        if chats.get(item["conversation_id"], {}).get("chatType") == "oneOnOne":
            from .sweeps.chieff import autonomous_reply
            try:
                r = await autonomous_reply(item, "C", True, professional=True)
                db.update_item(item["id"], status="held",
                               action_taken=f"chieff professional reply ({r})")
                return
            except actions.SendBlocked as e:
                log.info("vip concierge blocked, falling back to holding: %s", e)
    await _send_holding(item)


async def away_reply_pass(away_replies: bool):
    """Catch-up: once the user has been away past the threshold, reply to fresh
    messages that arrived before it was reached and are still unanswered."""
    if not away_replies:
        return
    max_age = int(policy()["triage"].get("auto_reply_max_age_minutes", 60))
    rows = db.query(
        "SELECT * FROM items WHERE source='teams_chat' AND type != 'chieff' "
        "AND tier IN ('A','C') AND status IN ('classified','drafted') "
        "AND received_at > ?", (db.cutoff(minutes=max_age),))
    if not rows:
        return
    item_ids = [item["id"] for item in rows]
    cls_batch = db.query(
        "SELECT item_id, detail FROM audit_log WHERE action='classified' "
        "AND item_id IN (%s) GROUP BY item_id HAVING id=MAX(id)" % ",".join("?" * len(item_ids)),
        tuple(item_ids))
    cls_by_id = {r["item_id"]: json.loads(r["detail"]) for r in cls_batch}
    for item in rows:
        d = cls_by_id.get(item["id"])
        if not d:
            continue
        if not d.get("needs_reply"):
            continue
        if not await _auto_engage_allowed(item, bool(d.get("waiting")), True):
            continue
        if item["tier"] == "A" and policy().get("chieff", {}).get("autonomy"):
            from .sweeps.chieff import autonomous_reply
            try:
                r = await autonomous_reply(item, "A", away=True)
                db.update_item(item["id"], status="auto_replied",
                               action_taken=f"chieff auto-replied ({r})")
            except actions.SendBlocked:
                pass
        elif item["tier"] == "C":
            await _tier_c_away_reply(item)


async def _send_holding(item: dict):
    msg_tpl = policy()["messaging"]["holding_message"]
    when = await estimate_return_time()
    try:
        r = await actions.auto_send_teams(
            item["conversation_id"], msg_tpl.format(time=when),
            kind="holding_message", item_id=item["id"], sender=item["sender"])
        db.update_item(item["id"], status="held", action_taken=f"holding message ({r})")
    except actions.SendBlocked as e:
        db.update_item(item["id"], action_taken=f"holding blocked: {e}")


async def holding_pass(away: bool):
    """Tier B: if the user is away and the sender is clearly waiting, send a holding
    message once the configured delay has passed without an approval."""
    if not away:
        return
    delay = policy()["messaging"].get("holding_message_delay_minutes", 15)
    rows = db.query(
        "SELECT i.* FROM items i JOIN drafts d ON d.item_id = i.id "
        "WHERE i.tier='B' AND i.status='drafted' AND d.status='pending' "
        "AND i.received_at < ?", (db.cutoff(minutes=int(delay)),))
    if not rows:
        return
    # Batch fetch all "sender was waiting" flags in one query.
    item_ids = [item["id"] for item in rows]
    waiting_set = {
        r["item_id"] for r in db.query(
            "SELECT item_id FROM audit_log WHERE action='classified' "
            "AND item_id IN (%s) AND json_extract(detail,'$.waiting')=1"
            % ",".join("?" * len(item_ids)), tuple(item_ids))
    }
    for item in rows:
        if item["id"] in waiting_set and await _auto_engage_allowed(item, True, True):
            await _send_holding(item)


async def online_reply_pass(away_now: bool):
    """The user is online (green): Chieff speaks in his name only when a pinged
    message has sat unread by the user for online_unseen_minutes. If the user has read
    the chat (viewpoint.lastMessageReadDateTime), Chieff leaves it to him."""
    if away_now:
        return
    cfg = policy().get("chieff", {})
    if not cfg.get("autonomy"):
        return
    unseen_min = int(cfg.get("online_unseen_minutes", 20))
    max_age = int(policy()["triage"].get("auto_reply_max_age_minutes", 60))
    rows = db.query(
        "SELECT * FROM items WHERE source='teams_chat' AND type != 'chieff' "
        "AND tier IN ('A','B') AND status IN ('classified','drafted') "
        "AND (action_taken IS NULL OR action_taken NOT LIKE 'chieff%') "
        "AND received_at > ? AND received_at < ?",
        (db.cutoff(minutes=max_age), db.cutoff(minutes=unseen_min)))
    if not rows:
        return
    chats = {ch["id"]: ch for ch in await graph.chats_cached()}
    # Batch-fetch latest classification for all items in one query (replaces N serial queries).
    item_ids = [item["id"] for item in rows]
    cls_batch = db.query(
        "SELECT item_id, detail FROM audit_log WHERE action='classified' "
        "AND item_id IN (%s) GROUP BY item_id HAVING id=MAX(id)" % ",".join("?" * len(item_ids)),
        tuple(item_ids))
    cls_by_id = {r["item_id"]: json.loads(r["detail"]) for r in cls_batch}
    for item in rows:
        d = cls_by_id.get(item["id"])
        if not d:
            continue
        if not d.get("needs_reply"):
            continue
        if not await _auto_engage_allowed(item, bool(d.get("waiting")), True):
            continue
        vp = (chats.get(item["conversation_id"]) or {}).get("viewpoint") or {}
        seen_s = vp.get("lastMessageReadDateTime")
        if seen_s:
            seen = datetime.fromisoformat(seen_s.replace("Z", "+00:00"))
            received = datetime.fromisoformat(item["received_at"].replace("Z", "+00:00"))
            if received.tzinfo is None:
                received = received.replace(tzinfo=timezone.utc)
            if seen >= received:
                continue  # the user saw it; his conversation
        from .sweeps.chieff import autonomous_reply
        try:
            r = await autonomous_reply(item, item["tier"])
            if item["tier"] == "A":
                db.update_item(item["id"], status="auto_replied",
                               action_taken=f"chieff replied, unseen {unseen_min}m+ ({r})")
            else:
                db.update_item(item["id"],
                               action_taken=f"chieff ack, unseen {unseen_min}m+ ({r})")
        except actions.SendBlocked:
            pass


async def triage_new_items() -> dict:
    presence = await state.get_presence()
    away_now = state.is_away(presence)
    state.note_presence_transition(away_now)
    # Chieff replies on the user's behalf only after he has been away this long.
    threshold = int(policy()["away"].get("reply_after_minutes", 20))
    away = away_now and state.away_minutes() >= threshold

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)).isoformat()
    db.execute(
        "UPDATE items SET status='dismissed', action_taken='aged out before triage' "
        "WHERE source='teams_chat' AND status='new' AND received_at < ?", (cutoff,))

    new_items = db.query(
        "SELECT * FROM items WHERE source='teams_chat' AND status='new' "
        "AND type != 'chieff' "  # @Chieff items are handled by sweeps.chieff
        "ORDER BY received_at ASC LIMIT ?", (BATCH_SIZE * MAX_BATCHES_PER_SWEEP,))

    # Build batches and classify them in parallel (batches are independent).
    batches = [new_items[i:i + BATCH_SIZE] for i in range(0, len(new_items), BATCH_SIZE)]

    async def _classify_one_batch(batch):
        try:
            return batch, await classify_batch(batch)
        except Exception as e:
            err_str = str(e)
            # On timeout: retry with a single-item batch (eliminates multi-item context
            # explosion as a timeout cause) — one more chance before giving up.
            if "timed out" in err_str.lower() and len(batch) > 1:
                log.warning("batch timeout on %d items; retrying one-by-one", len(batch))
                results = []
                for single in batch:
                    try:
                        _, r = await _classify_one_batch([single])
                        results.extend(r)
                    except Exception:
                        pass
                if results:
                    db.audit("triage_retry_ok", {"original_size": len(batch), "recovered": len(results)})
                    return batch, results
            log.exception("classification batch failed")
            db.audit("triage_error", {"error": err_str[:300]})
            return batch, []

    batch_results = await asyncio.gather(*[_classify_one_batch(b) for b in batches])

    # Collect all (item, classification) pairs across all batch results.
    all_pairs: list[tuple[dict, dict]] = []
    for batch, results in batch_results:
        by_id = {it["id"]: it for it in batch}
        for c in results:
            item = by_id.get(c["item_id"])
            if item:
                all_pairs.append((item, c))

    # Pre-fetch pending draft count once so each concurrent act_on_classification
    # doesn't issue its own COUNT query (all see the same backlog at this moment).
    initial_pending = db.query("SELECT COUNT(*) AS n FROM drafts WHERE status='pending'")[0]["n"]

    # Act on all classifications concurrently (separate conversations; each
    # action is rate-limited per conversation, so no cross-item interference).
    async def _act(item, c):
        await act_on_classification(item, c, away, pending_count=initial_pending)
        return 1

    counts = await asyncio.gather(*[_act(it, c) for it, c in all_pairs],
                                  return_exceptions=True)
    classified = sum(1 for r in counts if r == 1)
    for (item, _c), r in zip(all_pairs, counts):
        if isinstance(r, Exception):
            log.exception("act_on_classification failed for item %s", item.get("id"), exc_info=r)
            db.audit("triage_act_error", {"item_id": item.get("id"), "error": str(r)[:300]},
                     item_id=item.get("id"))

    await holding_pass(away)
    await away_reply_pass(away)
    await online_reply_pass(away_now)

    # Lightweight: fast-expire tier A no-reply items so the pings list stays clean.
    _tier_a_compact()
    # Post-batch sender compaction: race condition means concurrent act_on_classification
    # calls can bypass the per-sender cap. Clean up excess drafts after each sweep.
    _sender_compact()

    return {"classified": classified, "away": away_now,
            "away_replies_active": away}


def _tier_a_compact():
    """Dismiss tier A classified items older than the configured expiry window.
    Called every triage cycle (cheap DB query) — keeps active pings focused."""
    from .config import policy as _policy
    hours = int(_policy().get("hygiene", {}).get("tier_a_dismiss_hours", 8))
    cutoff = db.cutoff(hours=hours)
    db.execute(
        "UPDATE items SET status='dismissed', action_taken=?, updated_at=? "
        "WHERE tier='A' AND status='classified' "
        "AND source IN ('teams_chat','teams_channel') AND received_at < ?",
        (f"tier A compact (>{hours}h, no reply needed)", db.now(), cutoff))


def _sender_compact():
    """Post-batch dedup: keep at most max_drafts_per_sender pending drafts per sender.
    Concurrent act_on_classification calls bypass the per-item check (race condition),
    so this runs after each sweep to enforce the cap retroactively. Keeps the newest
    drafts; supersedes older ones so they're not deleted and the audit trail is intact."""
    from .config import policy as _policy
    max_per = int(_policy().get("triage", {}).get("max_drafts_per_sender", 2))

    # Find senders with more than max_per pending drafts and get the excess draft IDs.
    rows = db.query(
        "SELECT d.id, i.sender, d.created_at, d.channel, "
        "       ROW_NUMBER() OVER (PARTITION BY i.sender, d.channel ORDER BY d.created_at DESC) AS rn "
        "FROM drafts d JOIN items i ON i.id=d.item_id "
        "WHERE d.status='pending'")
    excess_ids = [r["id"] for r in rows if r["rn"] > max_per]
    if not excess_ids:
        return
    ph = ",".join("?" * len(excess_ids))
    db.execute(
        f"UPDATE drafts SET status='superseded', resolved_at=? WHERE id IN ({ph})",
        (db.now(), *excess_ids))
    db.execute(
        f"UPDATE items SET status='classified', action_taken=?, updated_at=? "
        f"WHERE id IN (SELECT item_id FROM drafts WHERE id IN ({ph}))",
        (f"draft superseded (sender cap={max_per})", db.now(), *excess_ids))
    db.audit("sender_compact", {"excess_superseded": len(excess_ids), "cap": max_per})
    log.info("sender_compact: superseded %d excess drafts (cap=%d/sender)", len(excess_ids), max_per)
