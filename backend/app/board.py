"""Consolidated board endpoint and Today panel actions."""

import re
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import db, graph, state, vault
from .config import policy

router = APIRouter(prefix="/api")


async def _safe(coro, default, what):
    """Run an MCP/graph call but never let one failure (e.g. an expired token)
    500 the whole board. Returns default and surfaces the issue in the payload."""
    try:
        return await coro, None
    except Exception as e:
        import logging
        logging.getLogger("chief.board").warning("board: %s failed: %s", what, str(e)[:160])
        return default, str(e)[:200]


@router.get("/board")
async def board():
    import asyncio as _asyncio
    today = date.today()
    note = vault.read_note(today)
    recent = None
    if note is None:
        r = vault.most_recent_note()
        recent = vault.read_note(r[0]) if r else None
    (presence, pres_err), (calendar, cal_err), (focus, _) = await _asyncio.gather(
        _safe(state.get_presence(), {}, "presence"),
        _safe(graph.calendar_events_cached(today), [], "calendar"),
        _safe(state.proposed_focus(), [], "focus"),
    )
    integration_error = pres_err or cal_err  # e.g. "No valid access token" -> re-auth ms-graph

    return {
        "presence": {
            "availability": presence.get("availability"),
            "activity": presence.get("activity"),
        },
        "away": state.is_away(presence) if presence else False,
        # 3 individual get_setting() calls → 1 batch query (populates the per-key cache too).
        **{k: v for k, v in _read_board_settings().items()},
        "integration_error": integration_error,
        "note": note,
        "fallback_note": recent,
        "calendar": calendar,
        "focus": focus,
        "pings": db.query(
            "SELECT * FROM items WHERE source IN ('teams_chat','teams_channel') "
            "AND (snoozed_until IS NULL OR snoozed_until <= ?) "
            "ORDER BY urgent DESC, received_at DESC LIMIT 40",
            (db.now(),)),
        "drafts": db.query(
            "SELECT d.*, i.sender, i.subject, i.content AS item_content, "
            "i.tier, i.urgent FROM drafts d "
            "LEFT JOIN items i ON i.id = d.item_id WHERE d.status='pending' "
            "ORDER BY COALESCE(i.urgent,0) DESC, "
            "CASE i.tier WHEN 'C' THEN 0 WHEN 'B' THEN 1 ELSE 2 END, "
            "d.id DESC LIMIT 100"),
        "open_loops": db.query(
            "SELECT * FROM items WHERE type='open_loop' AND status != 'done' "
            "ORDER BY received_at ASC LIMIT 50"),
        # 3 scalar queries → 1 combined subquery pass.
        **_board_scalars(),
    }


def _read_board_settings() -> dict:
    """Fetch away_override, dry_run, kill_switch in 1 query (was 3 get_setting() calls)."""
    s = db.get_settings_multi(["away_override", "dry_run", "kill_switch"])
    return {
        "away_override": s.get("away_override"),
        "dry_run": s.get("dry_run") == "true",
        "kill_switch": s.get("kill_switch") == "true",
    }


def _board_scalars() -> dict:
    """Fetch weekly_status_draft + snoozed_count + pr_readiness + osca_alerts in 1 pass."""
    now = db.now()
    row = db.query(
        "SELECT "
        "(SELECT COUNT(*) FROM weekly_reports WHERE status='draft') AS weekly_draft_n, "
        "(SELECT COUNT(*) FROM items WHERE snoozed_until > ?) AS snoozed_count, "
        "(SELECT value FROM settings WHERE key='pr_readiness_state' LIMIT 1) AS pr_raw, "
        "(SELECT COUNT(*) FROM osca_problems WHERE status='active' AND severity='high') "
        "  AS osca_alerts",
        (now,))[0]
    return {
        "weekly_status_draft": bool(row["weekly_draft_n"]),
        "snoozed_count": row["snoozed_count"] or 0,
        "pr_readiness": _pr_readiness_from_raw(row["pr_raw"]),
        "osca_alerts": row["osca_alerts"] or 0,
        # High-sev pipeline problems first seen in the last 30 min — the UI
        # fires a browser notification per key (deduped client-side).
        "osca_new": db.query(
            "SELECT key, title, severity, count, kind FROM osca_problems "
            "WHERE severity='high' AND status='active' AND first_seen > ? "
            "ORDER BY first_seen DESC LIMIT 5", (db.cutoff(minutes=30),)),
    }


def _pr_readiness_from_raw(raw: str | None) -> dict:
    import json as _json
    try:
        data = _json.loads(raw) if raw else {"prs": []}
    except Exception:
        data = {"prs": []}
    prs = data.get("prs", [])
    return {"prs": prs, "at": data.get("at"),
            "needs_attention": sum(1 for r in prs if r.get("needs_attention"))}


def _pr_readiness() -> dict:
    return _pr_readiness_from_raw(db.get_setting("pr_readiness_state"))


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


class CheckboxEdit(BaseModel):
    date: str
    line: int
    text: str


@router.post("/today/edit")
def edit_checkbox(body: CheckboxEdit):
    d = date.fromisoformat(body.date)
    if not vault.edit_checkbox_text(d, body.line, body.text):
        raise HTTPException(409, "note changed under us; refresh")
    db.audit("checkbox_edited", {"date": body.date, "line": body.line,
                                 "text": body.text[:200]}, actor="user")
    return {"ok": True, "note": vault.read_note(d)}


@router.get("/meetings")
async def meetings():
    """Today's and yesterday's meetings with what we can summarize from.

    Graph transcripts need OnlineMeetingTranscript.Read.All which the ms-graph
    server does not have, so transcript_available is honest about that; the
    meeting's Teams chat and vault notes are the fallback sources.
    """
    import asyncio as _asyncio
    today = date.today()
    yesterday = today - timedelta(days=1)
    # Fetch chats + both days of calendar events concurrently (was 3 sequential awaits).
    chats, events_today, events_yesterday = await _asyncio.gather(
        graph.chats_cached(),
        graph.calendar_events_cached(today),
        graph.calendar_events_cached(yesterday),
    )
    chat_by_topic = {
        (c.get("topic") or "").strip().lower(): c["id"]
        for c in chats if c.get("chatType") == "meeting"
    }
    out = []
    for d, events in ((today, events_today), (yesterday, events_yesterday)):
        note_text = ""
        if vault.note_path(d).exists():
            # ignore the auto-written meeting lists ("- 16:30 Subject") so only
            # real human/summary notes count as "notes in vault"
            note_text = "\n".join(
                ln for ln in vault.note_path(d).read_text().lower().splitlines()
                if not re.match(r"^\s*-\s+\d\d:\d\d\s", ln)
            )
        for e in events:
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


def _known_channels() -> list[dict]:
    """Channels CoS actually knows about: whatever's pinned in policy.yaml
    (pr_channel_review, pr_review.digest_channels) plus any teams_channel
    watcher the board has created. Not a full org-wide channel directory -
    just the ones CoS reads from or could write to (owner decision, 2026-07-16)."""
    out: dict[str, dict] = {}

    def _add(team_id: str, channel_id: str, label: str):
        if not team_id or not channel_id:
            return
        key = f"{team_id}|{channel_id}"
        out.setdefault(key, {"team_id": team_id, "channel_id": channel_id, "label": label})

    pcr = policy().get("pr_channel_review", {}) or {}
    _add(pcr.get("team_id"), pcr.get("channel_id"), "PR Reviews")
    for c in (policy().get("pr_review", {}).get("digest_channels") or []):
        _add(c.get("team_id"), c.get("channel_id"), c.get("name") or "GitHub digest")
    for w in db.query("SELECT name, target FROM watchers WHERE kind='teams_channel'"):
        if "|" in (w["target"] or ""):
            team_id, channel_id = w["target"].split("|", 1)
            _add(team_id, channel_id, w["name"])
    return list(out.values())


@router.get("/write-access")
async def write_access():
    """Every known chat AND channel with its effective read/write permission
    and where each comes from. Reading defaults to on everywhere (always has);
    writing is allowlist-only for chats and pinned-by-id for channels - board
    toggles here override either, per entity, independently (owner decision, 2026-07-16:
    read and write used to be a single "writable" toggle with no read gate
    at all; now both are real, separate switches)."""
    from . import actions
    _, write_removes = actions.board_write_overrides()
    _, read_removes = actions.board_read_overrides()
    # best-effort labels for 1:1 chats from swept items
    senders = {r["conversation_id"]: r["sender"] for r in db.query(
        "SELECT conversation_id, sender FROM items "
        "WHERE source='teams_chat' AND sender IS NOT NULL GROUP BY conversation_id")}
    out = []
    for c in await graph.chats_cached(ttl=60):
        cid = c["id"]
        label = c.get("topic") or senders.get(cid) or f"({c.get('chatType')} chat)"
        writable = await actions._teams_write_allowed(cid, senders.get(cid))
        preview = (c.get("lastMessagePreview") or {}).get("createdDateTime")
        out.append({
            "id": cid, "kind": "chat", "label": label, "type": c.get("chatType"),
            "readable": actions.read_allowed(cid), "writable": writable,
            "last_activity": preview,
        })
    for ch in _known_channels():
        key = actions.channel_key(ch["team_id"], ch["channel_id"])
        out.append({
            "id": key, "kind": "channel", "label": ch["label"], "type": "channel",
            "team_id": ch["team_id"], "channel_id": ch["channel_id"],
            "readable": actions.read_allowed(key),
            "writable": actions.channel_write_allowed(ch["team_id"], ch["channel_id"]),
            "last_activity": None,
        })
    out.sort(key=lambda x: (x["kind"] != "channel", x.get("last_activity") or ""), reverse=True)
    return out


class WriteAccessBody(BaseModel):
    id: str
    kind: str = "chat"  # "chat" | "channel"
    dimension: str = "write"  # "read" | "write"
    allow: bool
    label: str = ""


@router.post("/write-access")
def set_write_access(body: WriteAccessBody):
    import json as _json
    from . import actions
    setting_prefix = "teams_read" if body.dimension == "read" else "teams_write"
    adds = set(_json.loads(db.get_setting(f"{setting_prefix}_add", "[]")))
    removes = set(_json.loads(db.get_setting(f"{setting_prefix}_remove", "[]")))
    if body.allow:
        adds.add(body.id)
        removes.discard(body.id)
    else:
        adds.discard(body.id)
        removes.add(body.id)
    db.set_setting(f"{setting_prefix}_add", _json.dumps(sorted(adds)))
    db.set_setting(f"{setting_prefix}_remove", _json.dumps(sorted(removes)))
    db.audit("write_access_changed",
             {"entity": body.label or body.id, "id": body.id, "kind": body.kind,
              "dimension": body.dimension, "allow": body.allow},
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


class AddTaskBody(BaseModel):
    text: str
    section: str = "Also Do Today"  # or "Top 3 for Today", "Carry Forward"


@router.post("/today/add-task")
def add_task(body: AddTaskBody):
    """Quick-add a checkbox to today's daily note from the board."""
    today = date.today()
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "text is required")
    allowed_sections = {"Top 3 for Today", "Also Do Today", "Carry Forward"}
    if body.section not in allowed_sections:
        raise HTTPException(400, f"section must be one of: {sorted(allowed_sections)}")
    if not vault.note_path(today).exists():
        raise HTTPException(404, "no daily note for today yet — run morning brief first")
    vault.append_to_section(today, body.section, f"- [ ] {text}")
    db.audit("task_added", {"text": text, "section": body.section, "date": today.isoformat()},
             actor="user")
    from .events import broadcast
    broadcast("board_changed")
    return {"ok": True, "note": vault.read_note(today)}
