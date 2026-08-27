"""Pre-meeting context brief.

Fires every 5 minutes; identifies meetings starting in 25–45 minutes that
haven't been briefed yet and posts a focused prep note to the user's Teams
self-chat: who you're meeting, what you last discussed, pending open loops
and pings from each attendee.

Idempotent: one brief per meeting per day (keyed on date + normalized subject).
"""

import logging
from datetime import datetime, timedelta, timezone

from .. import actions, db, graph
from ..agents import CHIEF, run_agent
from ..config import policy

log = logging.getLogger("chief.sweep.pre_meeting")

BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "brief": {
            "type": "string",
            "description": "The full prep message to post (plain text, Teams HTML ok). "
                           "Under 300 words. No em dashes.",
        },
    },
    "required": ["brief"],
}

PROMPT = """You are Chieff, the user's chief of staff. Write a concise pre-meeting prep note for the user, to be posted in his Teams self-chat ~30 minutes before the meeting.

Meeting: {subject}
Time: {time_local}
Attendees: {attendees}

Recent Teams/email context with these attendees (from the board, last 14 days):
{context}

Open pings from attendees still awaiting reply:
{pings}

Open loops involving these attendees:
{loops}

Rules:
- Lead with the meeting name and time in bold.
- Then: who's coming (one line), what needs to go into the meeting (the most important thing to say or decide), and any open items from the board to resolve or mention.
- If there's nothing notable in the context, say so honestly ("no open threads with these attendees").
- Tight, factual, no filler. Under 200 words.
- No em dashes.
"""


def _window() -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    return now + timedelta(minutes=25), now + timedelta(minutes=45)


def _already_briefed(date_str: str, subject_key: str) -> bool:
    ext_id = f"pre_meeting:{date_str}:{subject_key}"
    rows = db.query(
        "SELECT 1 FROM audit_log WHERE action='pre_meeting_brief' "
        "AND json_extract(detail,'$.ext_id')=? LIMIT 1",
        (ext_id,))
    return bool(rows)


async def sweep() -> dict:
    cfg = policy().get("pre_meeting", {})
    if cfg.get("disabled"):
        return {"skipped": "pre_meeting.disabled in policy.yaml"}

    lo, hi = _window()
    today = lo.date()
    events = await graph.calendar_events_cached(today)
    me_email = policy()["me"]["email"].lower()

    candidates = []
    for e in events:
        subject = (e.get("subject") or "").strip()
        start_s = e.get("start_local") or e.get("start")
        if not subject or not start_s:
            continue
        try:
            start = datetime.fromisoformat(start_s)
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if not (lo <= start <= hi):
            continue
        # skip meetings with only the user (self-reminders, focus blocks)
        attendees = [
            a for a in (e.get("attendees") or [])
            if (a.get("emailAddress") or {}).get("address", "").lower() != me_email
        ]
        if not attendees:
            continue
        subject_key = subject.lower()
        if _already_briefed(today.isoformat(), subject_key):
            continue
        candidates.append((e, subject, subject_key, start, attendees))

    if not candidates:
        return {"checked": len(events), "briefed": 0}

    briefed = 0
    for e, subject, subject_key, start, attendees in candidates:
        ext_id = f"pre_meeting:{today.isoformat()}:{subject_key}"
        try:
            await _post_brief(subject, start, attendees, ext_id)
            briefed += 1
        except actions.SendBlocked as exc:
            log.info("pre-meeting brief blocked for '%s': %s", subject, exc)
        except Exception:
            log.exception("pre-meeting brief failed for '%s'", subject)

    return {"checked": len(events), "briefed": briefed}


async def _post_brief(subject: str, start: datetime,
                      attendees: list[dict], ext_id: str) -> None:
    names = [
        (a.get("emailAddress") or {}).get("name") or
        (a.get("emailAddress") or {}).get("address", "?")
        for a in attendees
    ]
    name_set = {n.lower() for n in names}
    addr_set = {
        (a.get("emailAddress") or {}).get("address", "").lower()
        for a in attendees
    }

    # Pull recent board context for these attendees (last 14 days of items/pings).
    cutoff = db.cutoff(days=14)
    ctx_rows = db.query(
        "SELECT source, sender, sender_id, subject, substr(content,1,200) AS content, "
        "status, received_at "
        "FROM items WHERE received_at >= ? AND status NOT IN ('dismissed') "
        "ORDER BY received_at DESC LIMIT 200",
        (cutoff,))
    ctx_lines = []
    ping_lines = []
    loop_lines = []
    for r in ctx_rows:
        sender = (r.get("sender") or "").lower()
        sender_id = (r.get("sender_id") or "").lower()
        if not (sender in name_set or sender_id in addr_set):
            continue
        snip = f"[{r['source']}] {r['subject'] or ''}: {r['content'] or ''}".strip()
        if r["source"] in ("teams_chat", "teams_channel", "email"):
            if r["status"] in ("classified", "drafted", "held"):
                ping_lines.append(snip[:200])
            else:
                ctx_lines.append(snip[:200])
        elif r["source"] == "open_loop":
            loop_lines.append(snip[:200])
        else:
            ctx_lines.append(snip[:200])

    time_local = start.astimezone().strftime("%H:%M")
    result = await run_agent(
        CHIEF,
        PROMPT.format(
            subject=subject,
            time_local=time_local,
            attendees=", ".join(names) if names else "(no external attendees)",
            context="\n".join(ctx_lines[:12]) or "(none)",
            pings="\n".join(ping_lines[:6]) or "(none)",
            loops="\n".join(loop_lines[:6]) or "(none)",
        ),
        schema=BRIEF_SCHEMA, with_tools=False, max_turns=4, label="pre_meeting_brief",
    )
    msg = result.get("brief", "").strip()
    if not msg:
        return

    await actions.notify_alex(msg, kind="pre_meeting_brief")
    db.audit("pre_meeting_brief", {
        "ext_id": ext_id,
        "subject": subject,
        "time_local": time_local,
        "attendees": names,
    })
    from .. import vault
    vault.chieff_trace("Calendar", f"pre-meeting brief posted: {subject} at {time_local}")
