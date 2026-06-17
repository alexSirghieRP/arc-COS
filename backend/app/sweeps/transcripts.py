"""Meeting summary sweep: EVERY ended meeting gets summarized into the daily
note. Source preference: transcript (needs the OnlineMeeting* scopes) > the
meeting's Teams chat > a minimal record when neither exists.

Until the user re-consents (cd ~/RP/mcp-ms-graph && node auth-node.js), transcript
fetches fail contained and the chat fallback covers everything.
"""

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


async def sweep() -> dict:
    if not policy().get("transcripts", {}).get("auto_summarize", True):
        return {"skipped": "disabled in policy.yaml"}

    now = datetime.now().astimezone()
    chats = await graph.list_chats(top=50)
    meeting_chats = {}
    for c in chats:
        if c.get("chatType") == "meeting":
            topic = (c.get("topic") or "").strip().lower()
            meeting_chats[topic] = {
                "chat_id": c["id"],
                "join_url": (c.get("onlineMeetingInfo") or {}).get("joinWebUrl"),
            }

    summarized, skipped = 0, 0
    for d in (date.today(), date.today() - timedelta(days=1)):
        for e in await graph.calendar_events(d):
            subject = (e.get("subject") or "").strip()
            end_local = e.get("end_local")
            if not subject or not end_local:
                continue
            if datetime.fromisoformat(end_local) > now - timedelta(minutes=5):
                continue  # not ended yet

            ext_id = f"{d.isoformat()}:{subject.lower()}"
            item_id, is_new = db.upsert_item(
                source="meeting_summary", type_="meeting_summary", external_id=ext_id,
                subject=subject, received_at=end_local,
                content=f"summary of {subject} on {d.isoformat()}")
            if not is_new:
                row = db.query("SELECT status FROM items WHERE id=?", (item_id,))[0]
                if row["status"] == "done":
                    skipped += 1
                    continue

            info = meeting_chats.get(subject.lower(), {})
            material, label = None, "none"
            if info.get("join_url"):
                material = await _transcript_material(info["join_url"])
                if material:
                    label = "transcript"
            if material is None and info.get("chat_id"):
                # transcript not pullable (no access or none exists): ask once
                # in the meeting's own chat for the recording/transcript link
                await _ask_for_transcript(info["chat_id"], subject, ext_id, item_id)
                material = await _chat_material(info["chat_id"])
                if material:
                    label = "chat"

            try:
                vault.create_note(d)
                # don't double-write if a summary for this meeting already exists
                # (e.g. from the board's manual summarize button)
                existing = vault.note_path(d).read_text()
                if f"{subject} (summary by Chief" in existing or f"{subject} (transcript summary" in existing:
                    db.update_item(item_id, status="done", action_taken="summary already in note")
                    skipped += 1
                    continue
                start = (e.get("start_local") or "")[11:16]
                if material is None:
                    block = (f"{subject} ({start}): no transcript, recording, or chat "
                             "activity available to summarize.")
                else:
                    source = ("meeting transcript" if label == "transcript"
                              else "Teams meeting chat (no transcript available)")
                    summary = await run_agent(
                        CHIEF, SUMMARY_PROMPT.format(subject=subject, day=d.isoformat(),
                                                     source=source, material=material),
                        with_tools=False, max_turns=4)
                    summary = (summary or "").strip()
                    if not summary:
                        raise RuntimeError("empty summary")
                    block = f"{subject} (summary by Chief, from {label}):\n{summary}"
                vault.append_as_chieff(d, "Notes / Decisions Today", block)
                db.update_item(item_id, status="done",
                               action_taken=f"summarized to daily note (source: {label})")
                db.audit("note_written", {"date": d.isoformat(), "meeting": subject,
                                          "source": label, "summary": block[:1500]},
                         item_id=item_id)
                vault.chieff_trace("Meetings", f"summarized '{subject}' (from {label}) "
                                               f"into the user's daily note")
                summarized += 1
            except Exception:
                log.exception("summary failed for %s", subject)
                db.update_item(item_id, status="new")  # retry next sweep

    log.info("meeting summaries: %d written, %d already done", summarized, skipped)
    return {"summarized": summarized, "already_done": skipped,
            "transcripts_auth_ok": db.get_setting("transcripts_auth_ok")}
