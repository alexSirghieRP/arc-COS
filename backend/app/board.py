"""Consolidated board endpoint and Today panel actions."""

import re
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import db, graph, state, vault

router = APIRouter(prefix="/api")


@router.get("/board")
async def board():
    presence = await state.get_presence()
    today = date.today()
    note = vault.read_note(today)
    recent = None
    if note is None:
        r = vault.most_recent_note()
        recent = vault.read_note(r[0]) if r else None

    return {
        "presence": {
            "availability": presence.get("availability"),
            "activity": presence.get("activity"),
        },
        "away": state.is_away(presence),
        "away_override": db.get_setting("away_override"),
        "dry_run": db.get_setting("dry_run") == "true",
        "kill_switch": db.get_setting("kill_switch") == "true",
        "note": note,
        "fallback_note": recent,
        "calendar": await graph.calendar_events(today),
        "focus": await state.proposed_focus(),
        "pings": db.query(
            "SELECT * FROM items WHERE source IN ('teams_chat','teams_channel') "
            "ORDER BY received_at DESC LIMIT 40"),
        "drafts": db.query(
            "SELECT d.*, i.sender, i.subject, i.content AS item_content FROM drafts d "
            "LEFT JOIN items i ON i.id = d.item_id WHERE d.status='pending' ORDER BY d.id DESC"),
        "open_loops": db.query(
            "SELECT * FROM items WHERE type='open_loop' AND status != 'done' "
            "ORDER BY received_at ASC LIMIT 50"),
        "audit": db.query("SELECT * FROM audit_log ORDER BY id DESC LIMIT 30"),
        "weekly_status_draft": bool(db.query(
            "SELECT 1 FROM weekly_reports WHERE status='draft' LIMIT 1")),
        "pr_readiness": _pr_readiness(),
    }


def _pr_readiness() -> dict:
    import json as _json
    raw = db.get_setting("pr_readiness_state")
    try:
        data = _json.loads(raw) if raw else {"prs": []}
    except Exception:
        data = {"prs": []}
    prs = data.get("prs", [])
    return {"prs": prs, "at": data.get("at"),
            "needs_attention": sum(1 for r in prs if r.get("needs_attention"))}


class CheckboxToggle(BaseModel):
    date: str
    line: int
    checked: bool


@router.post("/today/checkbox")
def toggle_checkbox(body: CheckboxToggle):
    d = date.fromisoformat(body.date)
    if not vault.toggle_checkbox(d, body.line, body.checked):
        raise HTTPException(409, "note changed under us; refresh")
    db.audit("checkbox_toggled", {"date": body.date, "line": body.line,
                                  "checked": body.checked}, actor="user")
    return {"ok": True, "note": vault.read_note(d)}


@router.get("/meetings")
async def meetings():
    """Today's and yesterday's meetings with what we can summarize from.

    Graph transcripts need OnlineMeetingTranscript.Read.All which the ms-graph
    server does not have, so transcript_available is honest about that; the
    meeting's Teams chat and vault notes are the fallback sources.
    """
    chats = await graph.list_chats(top=50)
    chat_by_topic = {
        (c.get("topic") or "").strip().lower(): c["id"]
        for c in chats if c.get("chatType") == "meeting"
    }
    out = []
    for d in (date.today(), date.today() - timedelta(days=1)):
        note_text = ""
        if vault.note_path(d).exists():
            # ignore the auto-written meeting lists ("- 16:30 Subject") so only
            # real human/summary notes count as "notes in vault"
            note_text = "\n".join(
                ln for ln in vault.note_path(d).read_text().lower().splitlines()
                if not re.match(r"^\s*-\s+\d\d:\d\d\s", ln)
            )
        for e in await graph.calendar_events(d):
            subject = (e.get("subject") or "").strip()
            out.append({
                **e,
                "day": d.isoformat(),
                "transcript_available": False,
                "chat_id": chat_by_topic.get(subject.lower()),
                "notes_in_vault": bool(subject) and subject.lower() in note_text,
            })
    return out


@router.get("/meetings/transcript")
async def meeting_transcript(chat_id: str):
    """The meeting's content as a transcript. Graph transcript artifacts need
    scopes the server lacks, so this is the meeting chat (speaker: line),
    oldest-first, which is what we summarize from anyway."""
    msgs = await graph.read_chat_messages(chat_id, top=200)
    msgs = sorted(msgs, key=lambda m: m.get("createdDateTime") or "")
    lines = []
    for m in msgs:
        if m.get("messageType") not in (None, "message"):
            continue
        who = ((m.get("from") or {}).get("user") or {}).get("displayName")
        body = graph.strip_html((m.get("body") or {}).get("content", ""))
        if who and body.strip():
            lines.append(f"{who}: {body.strip()}")
    return {"text": "\n".join(lines), "count": len(lines)}


@router.get("/write-access")
async def write_access():
    """Every known chat with its effective write permission and where it comes from."""
    from . import actions
    adds, removes = actions.board_write_overrides()
    # best-effort labels for 1:1 chats from swept items
    senders = {r["conversation_id"]: r["sender"] for r in db.query(
        "SELECT conversation_id, sender FROM items "
        "WHERE source='teams_chat' AND sender IS NOT NULL GROUP BY conversation_id")}
    out = []
    for c in await graph.chats_cached(ttl=60):
        cid = c["id"]
        label = c.get("topic") or senders.get(cid) or f"({c.get('chatType')} chat)"
        allowed = await actions._teams_write_allowed(cid, senders.get(cid))
        if cid in removes:
            source = "board (removed)"
        elif cid in adds:
            source = "board"
        elif allowed:
            source = "policy.yaml"
        else:
            source = None
        preview = (c.get("lastMessagePreview") or {}).get("createdDateTime")
        out.append({"chat_id": cid, "label": label, "type": c.get("chatType"),
                    "writable": allowed, "source": source, "last_activity": preview})
    out.sort(key=lambda x: x.get("last_activity") or "", reverse=True)
    return out


class WriteAccessBody(BaseModel):
    chat_id: str
    allow: bool
    label: str = ""


@router.post("/write-access")
def set_write_access(body: WriteAccessBody):
    import json as _json
    from . import actions
    adds, removes = actions.board_write_overrides()
    if body.allow:
        adds.add(body.chat_id)
        removes.discard(body.chat_id)
    else:
        adds.discard(body.chat_id)
        removes.add(body.chat_id)
    db.set_setting("teams_write_add", _json.dumps(sorted(adds)))
    db.set_setting("teams_write_remove", _json.dumps(sorted(removes)))
    db.audit("write_access_changed", {"chat": body.label or body.chat_id,
                                      "chat_id": body.chat_id, "allow": body.allow},
             actor="user")
    return {"ok": True}


class SummarizeBody(BaseModel):
    event_id: str
    day: str
    subject: str
    chat_id: str | None = None


@router.post("/meetings/summarize")
async def summarize_meeting(body: SummarizeBody):
    from .meetings import summarize_into_note
    return await summarize_into_note(body.day, body.subject, body.chat_id)
