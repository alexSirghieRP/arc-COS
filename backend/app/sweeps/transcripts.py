"""Meeting summary sweep: EVERY ended meeting gets summarized into the daily
note. Source preference: transcript (needs the OnlineMeeting* scopes) > the
meeting's Teams chat > a minimal record when neither exists.

Until the user re-consents (cd ~/RP/mcp-ms-graph && node auth-node.js), transcript
fetches fail contained and the chat fallback covers everything.
"""

import hashlib
import logging
import re
from datetime import date, datetime, timedelta

from .. import db, graph, vault
from ..agents import CHIEF, run_agent
from ..config import policy
from ..mcp_clients import get_manager

log = logging.getLogger("chief.sweep.meetings")

MAX_TRANSCRIPT_CHARS = 30000

SUMMARY_PROMPT = """Summarize this meeting for the user's daily note. 4 to 8 bullet lines:
decisions made, actions assigned (who/what), risks raised, open questions. If the
material is thin, fewer lines; do not invent content. Plain text bullets starting
with "- ". No em dashes, no headers, no preamble.

Meeting: {subject} on {day}

Source material ({source}):
{material}
"""


def _clean_vtt(vtt: str) -> str:
    """Drop WEBVTT headers, cue ids and timestamps; keep speaker lines."""
    lines = []
    for ln in vtt.splitlines():
        s = ln.strip()
        if not s or s == "WEBVTT" or "-->" in s or s.isdigit():
            continue
        lines.append(s)
    out, prev = [], None
    for ln in lines:
        if ln != prev:
            out.append(ln)
        prev = ln
    return "\n".join(out)


async def _transcript_material(join_url: str) -> str | None:
    """Returns cleaned transcript text, or None if unavailable. Sets auth flag."""
    try:
        artifacts = await get_manager().call(
            "ms-graph", "graph_get_meeting_artifacts", {"joinWebUrl": join_url})
    except Exception as ex:
        msg = str(ex)
        if "not granted" in msg or "403" in msg or "Forbidden" in msg:
            if db.get_setting("transcripts_auth_ok") != "false":
                db.set_setting("transcripts_auth_ok", "false")
                db.audit("transcripts_need_reauth", {
                    "hint": "run: cd ~/RP/mcp-ms-graph && node auth-node.js"})
        else:
            log.warning("artifacts call failed: %s", msg[:200])
        return None
    db.set_setting("transcripts_auth_ok", "true")
    if not isinstance(artifacts, dict) or not artifacts.get("found"):
        return None
    transcripts = artifacts.get("transcripts") or []
    if not isinstance(transcripts, list) or not transcripts:
        return None
    t = transcripts[-1]  # newest
    try:
        vtt = await get_manager().call(
            "ms-graph", "graph_get_transcript_content",
            {"meetingId": artifacts["meetingId"], "transcriptId": t["id"]})
        return _clean_vtt(str(vtt))[:MAX_TRANSCRIPT_CHARS] or None
    except Exception as ex:
        log.warning("transcript content failed: %s", str(ex)[:200])
        return None


async def _chat_material(chat_id: str) -> str | None:
    try:
        lines = []
        for m in await graph.read_chat_messages(chat_id, top=40):
            user = ((m.get("from") or {}).get("user") or {}).get("displayName")
            text = graph.strip_html((m.get("body") or {}).get("content", ""))
            if user and text:
                lines.append(f"{user}: {text[:400]}")
        lines.reverse()
        return "\n".join(lines) if lines else None
    except Exception as ex:
        log.warning("chat read failed: %s", str(ex)[:200])
        return None


async def _ask_for_transcript(chat_id: str, subject: str, ext_id: str, item_id: int):
    """Post one request in the meeting chat for the recording/transcript link."""
    if not policy().get("transcripts", {}).get("ask_in_chat", True):
        return
    already = db.query(
        "SELECT 1 FROM audit_log WHERE action='transcript_request' "
        "AND json_extract(detail, '$.meeting') = ? LIMIT 1", (ext_id,))
    if already:
        return
    from .. import actions
    msg = (f"Hi all, I could not pull the transcript or recording for "
           f"\"{subject}\". If anyone has the recording link or transcript, "
           f"could you post it here? It will be folded into the user's notes.")
    try:
        r = await actions.assistant_chat_post(chat_id, msg, kind="transcript_request_send",
                                              item_id=item_id)
        db.audit("transcript_request", {"meeting": ext_id, "chat_id": chat_id,
                                        "result": r}, item_id=item_id)
    except actions.SendBlocked as e:
        db.audit("transcript_request", {"meeting": ext_id, "blocked": str(e)},
                 item_id=item_id)


_ACTION_VERBS = re.compile(
    r'\b(will|to|should|needs?|must|shall|follow[- ]?up|review|check|send|share|'
    r'schedule|confirm|update|prepare|draft|write|look into|reach out|respond|reply)\b',
    re.I)
_ME_FIRST = ((policy().get("me", {}).get("name") or "").split() or [""])[0]
_ME_RE = re.compile(rf'\b{re.escape(_ME_FIRST)}\b', re.I) if _ME_FIRST else None


def _extract_owner_actions(summary: str, subject: str) -> list[str]:
    """Pull lines from a meeting summary that assign an action to the user
    (matched by first name from policy me.name).
    Returns verb-first action strings, deduped, max 5."""
    actions = []
    for raw in summary.splitlines():
        line = raw.strip().lstrip("- •*").strip()
        if len(line) < 15:
            continue
        if _ME_RE and _ME_RE.search(line) and _ACTION_VERBS.search(line):
            actions.append(line)
    return actions[:5]


def _upsert_action_loop(action: str, meeting_subject: str,
                        meeting_date: date, item_id: int) -> bool:
    """Create an open_loop for one meeting action item. Returns True if new."""
    key = hashlib.sha1(f"{meeting_date}:{action}".encode()).hexdigest()[:12]
    ext_id = f"meeting_action:{meeting_date.isoformat()}:{key}"
    _, is_new = db.upsert_item(
        source="open_loop", type_="open_loop", external_id=ext_id,
        subject=action[:200],
        content=f"Action from meeting '{meeting_subject}' on {meeting_date.isoformat()}",
        received_at=meeting_date.isoformat())
    if is_new:
        db.audit("loop_opened", {"loop": action[:200], "source": "meeting_summary",
                                 "meeting": meeting_subject}, item_id=item_id)
    return is_new


async def sweep() -> dict:
    if not policy().get("transcripts", {}).get("auto_summarize", True):
        return {"skipped": "disabled in policy.yaml"}

    import asyncio

    now = datetime.now().astimezone()
    chats, *day_events = await asyncio.gather(
        graph.chats_cached(),
        graph.calendar_events_cached(date.today()),
        graph.calendar_events_cached(date.today() - timedelta(days=1)),
    )
    events_by_day = {
        date.today(): day_events[0],
        date.today() - timedelta(days=1): day_events[1],
    }

    meeting_chats = {}
    for c in chats:
        if c.get("chatType") == "meeting":
            topic = (c.get("topic") or "").strip().lower()
            meeting_chats[topic] = {
                "chat_id": c["id"],
                "join_url": (c.get("onlineMeetingInfo") or {}).get("joinWebUrl"),
            }

    # Phase 1: Identify meetings needing summarization.
    # Pre-batch: 1 query to find already-done meetings (replaces N SELECT status per meeting).
    candidates = []  # (d, e, subject, ext_id, info)
    for d, events in events_by_day.items():
        for e in events:
            subject = (e.get("subject") or "").strip()
            end_local = e.get("end_local")
            if not subject or not end_local:
                continue
            if datetime.fromisoformat(end_local) > now - timedelta(minutes=5):
                continue
            ext_id = f"{d.isoformat()}:{subject.lower()}"
            candidates.append((d, e, subject, ext_id, meeting_chats.get(subject.lower(), {})))

    done_ext_ids: set[str] = set()
    if candidates:
        all_ext = [c[3] for c in candidates]
        ph = ",".join("?" * len(all_ext))
        done_ext_ids = {r["external_id"] for r in db.query(
            f"SELECT external_id FROM items WHERE source='meeting_summary' AND status='done' "
            f"AND external_id IN ({ph})", tuple(all_ext))}

    pending = []
    skipped = sum(1 for c in candidates if c[3] in done_ext_ids)
    for d, e, subject, ext_id, info in candidates:
        if ext_id in done_ext_ids:
            continue
        item_id, _ = db.upsert_item(
            source="meeting_summary", type_="meeting_summary", external_id=ext_id,
            subject=subject, received_at=e.get("end_local"),
            content=f"summary of {subject} on {d.isoformat()}")
        pending.append((d, e, subject, item_id, info, ext_id))

    if not pending:
        log.info("meeting summaries: 0 written, %d already done", skipped)
        return {"summarized": 0, "already_done": skipped,
                "transcripts_auth_ok": db.get_setting("transcripts_auth_ok")}

    # Phase 2: Fetch material for all pending meetings concurrently.
    async def _get_material(d, e, subject, item_id, info, ext_id):
        material, label = None, "none"
        if info.get("join_url"):
            material = await _transcript_material(info["join_url"])
            if material:
                label = "transcript"
        if material is None and info.get("chat_id"):
            await _ask_for_transcript(info["chat_id"], subject, ext_id, item_id)
            material = await _chat_material(info["chat_id"])
            if material:
                label = "chat"
        return (d, e, subject, item_id, ext_id, material, label)

    materialized = await asyncio.gather(*[_get_material(*m) for m in pending])

    # Phase 3: Run LLM summaries concurrently for meetings that have material.
    async def _summarize(d, e, subject, item_id, ext_id, material, label):
        if material is None:
            return (d, e, subject, item_id, label, None)
        try:
            source = ("meeting transcript" if label == "transcript"
                      else "Teams meeting chat (no transcript available)")
            summary = await run_agent(
                CHIEF, SUMMARY_PROMPT.format(subject=subject, day=d.isoformat(),
                                             source=source, material=material),
                with_tools=False, max_turns=4)
            return (d, e, subject, item_id, label, (summary or "").strip() or None)
        except Exception:
            log.exception("summary agent failed for %s", subject)
            return (d, e, subject, item_id, label, None)

    summarized_results = await asyncio.gather(*[_summarize(*m) for m in materialized])

    # Phase 4: Write to vault sequentially (file I/O must not interleave).
    summarized = 0
    for d, e, subject, item_id, label, summary in summarized_results:
        try:
            vault.create_note(d)
            existing = vault.note_path(d).read_text()
            if f"{subject} (summary by Chief" in existing or f"{subject} (transcript summary" in existing:
                db.update_item(item_id, status="done", action_taken="summary already in note")
                skipped += 1
                continue
            start = (e.get("start_local") or "")[11:16]
            if summary is None:
                block = (f"{subject} ({start}): no transcript, recording, or chat "
                         "activity available to summarize.")
            else:
                block = f"{subject} (summary by Chief, from {label}):\n{summary}"
            vault.append_as_chieff(d, "Notes / Decisions Today", block)
            db.update_item(item_id, status="done",
                           action_taken=f"summarized to daily note (source: {label})")
            db.audit("note_written", {"date": d.isoformat(), "meeting": subject,
                                      "source": label, "summary": block[:1500]},
                     item_id=item_id)
            # Extract the user's action items from the summary → open loops on the board.
            if summary:
                loops_created = sum(
                    1 for a in _extract_owner_actions(summary, subject)
                    if _upsert_action_loop(a, subject, d, item_id))
                if loops_created:
                    vault.chieff_trace("Meetings", f"auto-created {loops_created} open loop(s) "
                                                   f"from '{subject}' summary")
            vault.chieff_trace("Meetings", f"summarized '{subject}' (from {label}) "
                                           f"into the user's daily note")
            summarized += 1
        except Exception:
            log.exception("vault write failed for %s", subject)
            db.update_item(item_id, status="new")

    log.info("meeting summaries: %d written, %d already done", summarized, skipped)
    return {"summarized": summarized, "already_done": skipped,
            "transcripts_auth_ok": db.get_setting("transcripts_auth_ok")}
