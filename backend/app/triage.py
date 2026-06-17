"""Ping triage: rules + LLM classification, then guarded actions per tier."""

import json
import logging
from datetime import date, datetime, timedelta, timezone

from . import actions, db, graph, state
from .agents import CHIEF, run_agent
from .config import policy

log = logging.getLogger("chief.triage")

MAX_AGE_HOURS = 8          # older pings get surfaced but not auto-handled
BATCH_SIZE = 10
MAX_BATCHES_PER_SWEEP = 2

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

TRIAGE_PROMPT = """Classify each incoming Teams message for the user.

Tiers:
- A (trivial): status questions, "where is X", scheduling logistics, FYIs needing only an acknowledgement. proposed_reply should contain the actual short answer if it can be answered from the message alone, else a brief acknowledgement.
- B (routine but substantive): real work questions that deserve a considered reply. proposed_reply should be a solid draft reply for the user to approve.
- C (important or sensitive): anything from a VIP (flagged below), anything emotional or negative in tone, about money, HR, personnel, deadlines at risk, or production incidents. proposed_reply should still be drafted for the user to review, but it will never be sent automatically.

Set urgent=true only for things that genuinely cannot wait (production down, an angry client, a hard deadline today).
Set sender_is_waiting=true if the sender clearly expects a reply soon (direct question to the user, "?", "can you", time pressure).
Messages that are pure FYI or group chatter the user is not addressed in: needs_reply=false, proposed_reply="".
reasoning: one sentence explaining the tier, written so the user can audit the call later.
Reply style: brief, friendly, plain. Never use em dashes.

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
        events = await graph.calendar_events(date.today())
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


async def classify_batch(items: list[dict]) -> list[dict]:
    payload = [{
        "item_id": it["id"],
        "chat": it["subject"],
        "sender": it["sender"],
        "sender_is_vip": is_vip(it["sender"], it["sender_id"]),
        "mentioned_alex": it["type"] == "mention",
        "message": (it["content"] or "")[:1500],
    } for it in items]
    result = await run_agent(
        CHIEF,
        TRIAGE_PROMPT.replace("{items}", json.dumps(payload, ensure_ascii=False, indent=1)),
        schema=TRIAGE_SCHEMA, with_tools=False, max_turns=4, label="triage",
    )
    return result["classifications"]


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


async def act_on_classification(item: dict, c: dict, away: bool):
    """Apply tier policy. Rule layer hard-overrides: VIP is always C."""
    tier = c["tier"]
    if is_vip(item["sender"], item["sender_id"]):
        tier = "C"
    urgent = bool(c["urgent"])
    db.update_item(item["id"], tier=tier, tier_reasoning=c["reasoning"],
                   urgent=int(urgent), status="classified")
    db.audit("classified", {
        "tier": tier, "reasoning": c["reasoning"], "sender": item["sender"],
        "needs_reply": c["needs_reply"], "waiting": c["sender_is_waiting"],
        "urgent": urgent, "conversation": item["subject"],
    }, item_id=item["id"])

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

    # Tier B and C both get a draft for the approval queue.
    if c["proposed_reply"]:
        draft_id = db.execute(
            "INSERT INTO drafts(item_id, channel, recipient, reply_to_ref, body, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (item["id"], "teams", item["sender"], chat_id, c["proposed_reply"], db.now()))
        db.update_item(item["id"], status="drafted", action_taken="draft awaiting approval")
        db.audit("draft_created", {"draft_id": draft_id, "body": c["proposed_reply"],
                                   "conversation_id": chat_id}, item_id=item["id"])

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
    for item in rows:
        cls = db.query(
            "SELECT detail FROM audit_log WHERE item_id=? AND action='classified' "
            "ORDER BY id DESC LIMIT 1", (item["id"],))
        if not cls:
            continue
        d = json.loads(cls[0]["detail"])
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
    for item in rows:
        waiting = db.query(
            "SELECT 1 FROM audit_log WHERE item_id=? AND action='classified' "
            "AND json_extract(detail,'$.waiting')=1", (item["id"],))
        if waiting and await _auto_engage_allowed(item, True, True):
            await _send_holding(item)


async def online_reply_pass(away_now: bool):
    """the user is online (green): Chieff speaks in his name only when a pinged
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
    chats = {ch["id"]: ch for ch in await graph.list_chats(top=50)}
    for item in rows:
        cls = db.query(
            "SELECT detail FROM audit_log WHERE item_id=? AND action='classified' "
            "ORDER BY id DESC LIMIT 1", (item["id"],))
        if not cls:
            continue
        d = json.loads(cls[0]["detail"])
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
    classified = 0
    for i in range(0, len(new_items), BATCH_SIZE):
        batch = new_items[i:i + BATCH_SIZE]
        by_id = {it["id"]: it for it in batch}
        try:
            results = await classify_batch(batch)
        except Exception as e:
            log.exception("classification batch failed")
            db.audit("triage_error", {"error": str(e)[:300]})
            continue
        for c in results:
            item = by_id.get(c["item_id"])
            if item:
                await act_on_classification(item, c, away)
                classified += 1

    await holding_pass(away)
    await away_reply_pass(away)
    await online_reply_pass(away_now)
    return {"classified": classified, "away": away_now,
            "away_replies_active": away}
