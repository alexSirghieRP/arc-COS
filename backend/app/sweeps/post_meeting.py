"""Post-meeting catch-up sweep.

Runs every 3 minutes. When a calendar event ended in the last 8 minutes,
posts a brief "while you were in {meeting}" digest to the user's self-chat showing
what arrived in Teams during the meeting window. Idempotent: one digest per
meeting per day, keyed on the meeting ext_id.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from .. import db, graph, vault
from ..config import policy

log = logging.getLogger("chief.sweep.post_meeting")

_LOOK_BACK_MIN = 8   # meeting must have ended in this window
_BRIEF_WINDOW = 600  # seconds: messages from up to 10 min before meeting end


async def sweep() -> dict:
    cfg = policy().get("post_meeting", {})
    if cfg.get("disabled"):
        return {"skipped": "post_meeting.disabled in policy.yaml"}

    now = datetime.now(timezone.utc)
    today = now.date()
    events = await graph.calendar_events_cached(today)
    me_email = policy()["me"]["email"].lower()

    candidates = []
    for e in events:
        if e.get("isAllDay"):
            continue
        end_local = e.get("end_local")
        if not end_local:
            continue
        end_dt = datetime.fromisoformat(end_local)
        if end_dt.tzinfo is None:
            end_dt = end_dt.astimezone(timezone.utc)
        mins_since_end = (now - end_dt).total_seconds() / 60
        if not (0 < mins_since_end <= _LOOK_BACK_MIN):
            continue
        attendees = e.get("attendees") or []
        has_others = any(
            (a.get("emailAddress") or {}).get("address", "").lower() != me_email
            for a in attendees if (a.get("emailAddress") or {}).get("address")
        )
        if not has_others:
            continue
        start_local = e.get("start_local")
        start_dt = datetime.fromisoformat(start_local) if start_local else end_dt - timedelta(hours=1)
        if start_dt.tzinfo is None:
            start_dt = start_dt.astimezone(timezone.utc)
        candidates.append((e, start_dt, end_dt))

    if not candidates:
        return {"checked": 0, "posted": 0}

    posted = 0
    for e, start_dt, end_dt in candidates:
        subject = (e.get("subject") or "").strip()
        ext_id = f"post_meeting:{today.isoformat()}:{subject.lower()[:60]}"
        already = db.query(
            "SELECT 1 FROM audit_log WHERE action='post_meeting_brief' "
            "AND json_extract(detail,'$.ext_id')=? LIMIT 1", (ext_id,))
        if already:
            continue

        # Collect Teams messages received while the meeting was running.
        try:
            chats = await graph.chats_cached()
        except Exception as ex:
            log.warning("post_meeting: chats fetch failed: %s", ex)
            continue

        window_start = (start_dt - timedelta(seconds=60)).isoformat()
        window_end = (end_dt + timedelta(seconds=30)).isoformat()

        # Gather recent items from DB that arrived during the meeting window.
        rows = db.query(
            "SELECT sender, subject, content, source FROM items "
            "WHERE source IN ('teams_chat','teams_channel') "
            "AND received_at > ? AND received_at < ? "
            "ORDER BY received_at ASC LIMIT 20",
            (window_start, window_end))

        if not rows:
            db.audit("post_meeting_brief", {
                "ext_id": ext_id, "meeting": subject, "posted": False,
                "reason": "no messages during meeting"})
            continue

        lines = []
        by_sender: dict[str, list[str]] = {}
        for r in rows:
            sndr = r["sender"] or "someone"
            msg = (r["content"] or "")[:120].strip()
            if msg:
                by_sender.setdefault(sndr, []).append(msg)
        for sndr, msgs in by_sender.items():
            sample = msgs[0][:100]
            extra = f" (+{len(msgs)-1} more)" if len(msgs) > 1 else ""
            lines.append(f"- **{sndr}**: {sample}{extra}")

        if not lines:
            continue

        dur_min = round((end_dt - start_dt).total_seconds() / 60)
        summary_lines = "\n".join(lines[:8])
        body = (
            f"While you were in **{subject}** ({dur_min} min), "
            f"{len(rows)} message(s) came in:\n\n{summary_lines}"
        )

        try:
            from .. import actions
            self_chat = await actions._resolve_self_chat()
            if not self_chat:
                log.warning("post_meeting: self-chat not found; skipping")
                continue
            result = await actions.assistant_chat_post(
                self_chat, body, kind="post_meeting_brief", item_id=None)
            db.audit("post_meeting_brief", {
                "ext_id": ext_id, "meeting": subject,
                "msg_count": len(rows), "posted": True, "result": result})
            vault.chieff_trace("Meetings",
                               f"catch-up posted for '{subject}': {len(rows)} messages while in meeting")
            posted += 1
        except Exception as ex:
            log.warning("post_meeting: post failed for '%s': %s", subject, str(ex)[:200])

    return {"checked": len(candidates), "posted": posted}
