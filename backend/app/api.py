"""Board API: items, drafts, audit, settings, manual triggers."""

import asyncio
import json as _json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import db

log = logging.getLogger("chief.api")

router = APIRouter(prefix="/api")


# ---- Server-Sent Events: real-time push to connected browser tabs -----------

@router.get("/stream")
async def stream(request: Request):
    """SSE stream: pushes board_changed events so the UI never polls blindly."""
    from .events import broadcast, register, unregister
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    register(q)

    async def generate():
        try:
            yield 'data: {"type":"connected"}\n\n'
            while True:
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=22.0)
                    yield f"data: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            unregister(q)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/items")
def items(status: str | None = None, limit: int = 100):
    if status:
        return db.query(
            "SELECT * FROM items WHERE status=? ORDER BY received_at DESC LIMIT ?",
            (status, limit))
    return db.query("SELECT * FROM items ORDER BY received_at DESC LIMIT ?", (limit,))


_connector_cache: dict = {"ts": 0.0, "data": None}
_CONNECTOR_TTL = 30  # seconds; probe latency doesn't need to be millisecond-fresh


@router.get("/connectors")
async def connectors(refresh: bool = False):
    import time
    if not refresh and _connector_cache["data"] and time.time() - _connector_cache["ts"] < _CONNECTOR_TTL:
        return _connector_cache["data"]
    from .connectors import statuses
    result = await statuses()
    _connector_cache["ts"] = time.time()
    _connector_cache["data"] = result
    return result


@router.post("/connectors/{name}/reconnect")
async def reconnect_connector(name: str):
    """Restart one MCP server's stdio session from the board."""
    from . import mcp_clients
    mgr = mcp_clients.get_manager()
    if name not in mgr.server_names:
        raise HTTPException(404, f"known MCP servers: {mgr.server_names}")
    status = await mgr.reconnect(name)
    _connector_cache["ts"] = 0.0  # force fresh status on next poll
    db.audit("connector_reconnect", {"server": name, "status": status}, actor="user")
    return {"ok": status == "ok", "status": status}


_health_cache: dict = {"ts": 0.0, "data": None}
_HEALTH_TTL = 15  # job/agent health aggregates over thousands of rows; 15s staleness is fine


@router.get("/health")
def health(refresh: bool = False):
    """Agentic health: per-job runs, MCP session stats, agent-run health, DB.
    Cached 15s — avoids scanning 2000 job_run rows on every board_changed event."""
    import time as _tm
    if not refresh and _health_cache["data"] and _tm.time() - _health_cache["ts"] < _HEALTH_TTL:
        return _health_cache["data"]
    from .health import summary
    result = summary()
    _health_cache["data"] = result
    _health_cache["ts"] = _tm.time()
    return result


@router.get("/logs")
def logs(level: str | None = None, q: str | None = None,
         limit: int = 300, after_id: int | None = None):
    """Tail of the backend's in-memory log buffer (filterable)."""
    from .health import tail_logs
    return tail_logs(level=level, q=q, limit=min(limit, 1000), after_id=after_id)


class ItemStatus(BaseModel):
    status: str


@router.post("/items/{item_id}/status")
def set_item_status(item_id: int, payload: ItemStatus):
    """HITL: dismiss / mark done / reopen an item from the board."""
    allowed = {"dismissed", "done", "new"}
    if payload.status not in allowed:
        raise HTTPException(400, f"settable statuses: {sorted(allowed)}")
    rows = db.query("SELECT id, status FROM items WHERE id=?", (item_id,))
    if not rows:
        raise HTTPException(404, "no item with that id")
    db.update_item(item_id, status=payload.status)
    db.audit("item_status_changed",
             {"item_id": item_id, "from": rows[0]["status"], "to": payload.status},
             item_id=item_id, actor="user")
    from .events import broadcast
    broadcast("board_changed")
    return {"ok": True}


class SnoozeBody(BaseModel):
    hours: float  # 0 = cancel snooze


class BulkStatus(BaseModel):
    ids: list[int]
    status: str


@router.post("/items/bulk-status")
def bulk_status(payload: BulkStatus):
    """Bulk dismiss or mark done for multiple pings at once."""
    allowed = {"dismissed", "done"}
    if payload.status not in allowed:
        raise HTTPException(400, f"bulk settable: {sorted(allowed)}")
    if not payload.ids:
        return {"ok": True, "updated": 0}
    ph = ",".join("?" * len(payload.ids))
    existing = db.query(f"SELECT id FROM items WHERE id IN ({ph})", tuple(payload.ids))
    if not existing:
        return {"ok": True, "updated": 0}
    valid_ids = [r["id"] for r in existing]
    id_ph = ",".join("?" * len(valid_ids))
    ts = db.now()
    db.execute(
        f"UPDATE items SET status=?, updated_at=? WHERE id IN ({id_ph})",
        (payload.status, ts) + tuple(valid_ids))
    db.audit("bulk_status_changed", {"count": len(valid_ids), "to": payload.status}, actor="user")
    from .events import broadcast
    broadcast("board_changed")
    return {"ok": True, "updated": len(valid_ids)}


@router.post("/items/wake-snoozed")
def wake_snoozed():
    """Cancel all active snoozes so every snoozed ping reappears immediately."""
    now = db.now()
    rows = db.query(
        "SELECT id FROM items WHERE snoozed_until IS NOT NULL AND snoozed_until > ?",
        (now,))
    if rows:
        ids = [r["id"] for r in rows]
        ph = ",".join("?" * len(ids))
        db.execute(f"UPDATE items SET snoozed_until=NULL, updated_at=? WHERE id IN ({ph})",
                   (now,) + tuple(ids))
    db.audit("bulk_wake_snoozed", {"count": len(rows)}, actor="user")
    from .events import broadcast
    broadcast("board_changed")
    return {"ok": True, "woken": len(rows)}


@router.post("/items/{item_id}/snooze")
def snooze_item(item_id: int, payload: SnoozeBody):
    """Defer a ping from the active list for N hours (0 = cancel snooze)."""
    from datetime import datetime, timedelta, timezone
    rows = db.query("SELECT id FROM items WHERE id=?", (item_id,))
    if not rows:
        raise HTTPException(404, "no item with that id")
    if payload.hours <= 0:
        db.update_item(item_id, snoozed_until=None)
        snoozed_until = None
    else:
        snoozed_until = (
            datetime.now(timezone.utc) + timedelta(hours=payload.hours)
        ).isoformat()
        db.update_item(item_id, snoozed_until=snoozed_until)
    db.audit("item_snoozed", {"item_id": item_id, "hours": payload.hours,
                               "until": snoozed_until}, item_id=item_id, actor="user")
    from .events import broadcast
    broadcast("board_changed")
    return {"ok": True, "snoozed_until": snoozed_until}


@router.get("/search")
def search(q: str, limit: int = 30, include_obsidian: bool = False):
    """Full-text search across items (sender, subject, content), audit log, and optionally Obsidian vault."""
    if not q or len(q.strip()) < 2:
        raise HTTPException(400, "q must be at least 2 characters")
    needle = f"%{q.strip()}%"
    items = db.query(
        "SELECT id, source, type, sender, subject, content, status, tier, received_at "
        "FROM items WHERE sender LIKE ? OR subject LIKE ? OR content LIKE ? "
        "ORDER BY received_at DESC LIMIT ?",
        (needle, needle, needle, limit))
    audit_rows = db.query(
        "SELECT id, ts, action, actor, detail FROM audit_log "
        "WHERE action LIKE ? OR detail LIKE ? ORDER BY id DESC LIMIT ?",
        (needle, needle, min(limit, 20)))
    result = {"items": items, "audit": audit_rows, "q": q}
    if include_obsidian:
        try:
            from .obsidian import recent_notes
            qlow = q.lower()
            notes = [
                n for n in recent_notes(200)
                if qlow in n.get("name", "").lower() or qlow in (n.get("preview") or "").lower()
            ][:10]
            result["notes"] = notes
        except Exception:
            result["notes"] = []
    return result


@router.get("/audit")
def audit(limit: int = 200, after_id: int | None = None):
    if after_id:
        rows = db.query(
            "SELECT * FROM audit_log WHERE id > ? ORDER BY id ASC LIMIT ?",
            (after_id, min(limit, 1000)))
        rows = list(reversed(rows))  # newest-first for consistent ordering
    else:
        rows = db.query("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (min(limit, 1000),))
    last_id = rows[0]["id"] if rows else None  # rows[0] is newest after reversal / DESC
    return {"log": rows, "last_id": last_id}


@router.get("/settings")
def settings():
    return {r["key"]: r["value"] for r in db.query("SELECT key, value FROM settings")}


class SettingUpdate(BaseModel):
    key: str
    value: str


@router.post("/settings")
def set_setting(body: SettingUpdate):
    allowed = {"dry_run", "kill_switch", "away_override", "pr_review_paused"}
    if body.key not in allowed:
        raise HTTPException(400, f"settable keys: {sorted(allowed)}")
    db.set_setting(body.key, body.value)
    db.audit("setting_changed", {"key": body.key, "value": body.value}, actor="user")
    return {"ok": True}


class ApproveBody(BaseModel):
    body: str | None = None


class RejectBody(BaseModel):
    reason: str = ""


async def _retry_draft_with_feedback(draft: dict, rejection_reason: str) -> None:
    """Re-generate a draft incorporating the user's rejection feedback.
    The corrected draft goes straight to 'pending' so it appears in Approvals."""
    from .agents import CHIEF, run_agent
    RETRY_SCHEMA = {
        "type": "object",
        "properties": {"reply": {"type": "string"}},
        "required": ["reply"],
    }
    item_rows = db.query("SELECT * FROM items WHERE id=?", (draft["item_id"],))
    if not item_rows:
        return
    item = item_rows[0]
    prompt = (
        f"The user rejected a draft reply and gave this feedback: \"{rejection_reason}\"\n\n"
        f"Original message from {item['sender']}:\n{(item['content'] or '')[:1200]}\n\n"
        f"The rejected draft was:\n{(draft['body'] or '')[:800]}\n\n"
        f"Write an improved reply that directly addresses the user's feedback. "
        f"The user is direct and states opinions plainly. When they disagree, they say so with a specific reason — no diplomatic hedging, no softening. "
        f"If the feedback says 'push back harder', write something like 'No. Here's why: ...' — not 'I see the concern but...'. "
        f"First person. No em dashes. No corporate filler. No 'happy to help'."
    )
    result = await run_agent(
        CHIEF, prompt, schema=RETRY_SCHEMA, with_tools=False, max_turns=4,
        label="draft_retry")
    new_body = (result.get("reply") or "").strip()
    if not new_body or new_body == (draft["body"] or "").strip():
        return  # no improvement, skip
    new_id = db.execute(
        "INSERT INTO drafts(item_id, channel, recipient, reply_to_ref, body, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (draft["item_id"], draft["channel"], draft["recipient"],
         draft["reply_to_ref"], new_body, db.now()))
    db.update_item(draft["item_id"], status="drafted",
                   action_taken=f"retried draft after rejection: {rejection_reason[:80]}")
    db.audit("draft_retried", {"draft_id": new_id, "rejection_reason": rejection_reason,
                               "body": new_body}, item_id=draft["item_id"])


@router.post("/drafts/{draft_id}/approve")
async def approve_draft(draft_id: int, payload: ApproveBody):
    from . import actions
    rows = db.query("SELECT * FROM drafts WHERE id=? AND status='pending'", (draft_id,))
    if not rows:
        raise HTTPException(404, "no pending draft with that id")
    draft = rows[0]
    original_body = draft["body"] or ""
    edited = bool(payload.body and payload.body.strip() and payload.body.strip() != original_body.strip())
    if edited:
        draft["body"] = payload.body
        db.execute("UPDATE drafts SET body=? WHERE id=?", (payload.body, draft_id))
    # Pass edited flag so actions.py can include it in the audit trail.
    draft["_edited"] = edited
    try:
        result = await actions.approved_send(draft)
    except actions.SendBlocked as e:
        raise HTTPException(409, str(e))
    db.execute("UPDATE drafts SET status='approved_sent', resolved_at=? WHERE id=?",
               (db.now(), draft_id))
    if draft["item_id"]:
        db.update_item(draft["item_id"], status="answered",
                       action_taken=f"approved and {result}")
    from .events import broadcast
    broadcast("board_changed", {"draft": draft_id, "result": result})
    return {"ok": True, "result": result, "edited": edited}


@router.post("/drafts/{draft_id}/dismiss")
async def dismiss_draft(draft_id: int):
    """Silently supersede a draft — no feedback loop, no re-classify.
    Use for noise drafts that should never have been created."""
    rows = db.query("SELECT * FROM drafts WHERE id=? AND status='pending'", (draft_id,))
    if not rows:
        raise HTTPException(404, "no pending draft with that id")
    draft = rows[0]
    db.execute("UPDATE drafts SET status='superseded', resolved_at=? WHERE id=?",
               (db.now(), draft_id))
    if draft["item_id"]:
        db.update_item(draft["item_id"], status="dismissed",
                       action_taken="draft dismissed (noise)")
    db.audit("draft_dismissed", {"draft_id": draft_id, "body_preview": (draft["body"] or "")[:120]},
             item_id=draft["item_id"], actor="user")
    from .events import broadcast
    broadcast("board_changed", {"draft": draft_id})
    return {"ok": True}


@router.post("/drafts/{draft_id}/reject")
async def reject_draft(draft_id: int, payload: RejectBody):
    rows = db.query("SELECT * FROM drafts WHERE id=? AND status='pending'", (draft_id,))
    if not rows:
        raise HTTPException(404, "no pending draft with that id")
    draft = rows[0]
    db.execute("UPDATE drafts SET status='rejected', reject_reason=?, resolved_at=? WHERE id=?",
               (payload.reason, db.now(), draft_id))
    if draft["item_id"]:
        db.update_item(draft["item_id"], status="classified",
                       action_taken="draft rejected")
    db.audit("draft_rejected", {"draft_id": draft_id, "reason": payload.reason,
                                "body": draft["body"]},
             item_id=draft["item_id"], actor="user")

    # If a rejection reason was given, auto-generate a corrected draft so the user
    # doesn't have to manually trigger a re-classify. The retried draft goes
    # back to 'pending' with the feedback baked into the prompt.
    if payload.reason.strip() and draft["item_id"]:
        try:
            await _retry_draft_with_feedback(draft, payload.reason.strip())
        except Exception as e:
            log.warning("auto-retry draft failed: %s", e)

    from .events import broadcast
    broadcast("board_changed", {"draft": draft_id})
    return {"ok": True}


class BulkDismissPayload(BaseModel):
    tier: str = "B"       # which tier to bulk-dismiss (default: B-only)
    older_than_hours: int = 24  # only drafts older than this


@router.post("/drafts/bulk-dismiss-stale")
async def bulk_dismiss_stale_drafts(payload: BulkDismissPayload):
    """Dismiss pending B-tier drafts older than N hours. Safe: only B-tier by default.
    Tier C drafts are never bulk-dismissed; call individually after review."""
    if payload.tier not in ("A", "B"):
        raise HTTPException(400, "bulk dismiss only allowed for tier A or B")
    cutoff = db.cutoff(hours=payload.older_than_hours)
    stale = db.query(
        "SELECT d.id, d.item_id FROM drafts d JOIN items i ON i.id=d.item_id "
        "WHERE d.status='pending' AND i.tier=? AND d.created_at < ?",
        (payload.tier, cutoff))
    if not stale:
        return {"dismissed": 0}
    ids = [r["id"] for r in stale]
    ph = ",".join("?" * len(ids))
    with db.transaction():
        db.execute(f"UPDATE drafts SET status='superseded', resolved_at=? WHERE id IN ({ph})",
                   (db.now(), *ids))
        db.execute(
            f"UPDATE items SET status='dismissed', action_taken=?, updated_at=? "
            f"WHERE id IN (SELECT item_id FROM drafts WHERE id IN ({ph}))",
            (f"bulk dismissed (tier {payload.tier} > {payload.older_than_hours}h)", db.now(), *ids))
    db.audit("drafts_bulk_dismissed",
             {"count": len(ids), "tier": payload.tier, "older_than_hours": payload.older_than_hours,
              "draft_ids": ids[:50]}, actor="user")
    from .events import broadcast
    broadcast("board_changed", {"bulk_dismissed": len(ids)})
    return {"dismissed": len(ids)}


class LoopsRunPayload(BaseModel):
    ids: list[int]


@router.post("/loops/run")
async def loops_run(payload: LoopsRunPayload):
    if not payload.ids:
        raise HTTPException(400, "no loop ids selected")
    from .sweeps.open_loops import run_selected
    return await run_selected(payload.ids)


_quality_cache: dict = {"ts": 0.0, "data": None}
_QUALITY_TTL = 60  # quality stats accumulate slowly; 1-minute staleness is fine


@router.get("/quality")
def quality_stats(refresh: bool = False):
    """Draft quality metrics: approval rate, edit rate, rejection rate — the
    quantitative signal for triage calibration. All-time and last-30-days.
    3 audit_log scans → 1 combined SUM(CASE WHEN) query; 60s TTL cache."""
    import time as _tm
    if not refresh and _quality_cache["data"] and _tm.time() - _quality_cache["ts"] < _QUALITY_TTL:
        return _quality_cache["data"]

    cutoff_30d = db.cutoff(days=30)
    agg = db.query("""
        SELECT
          SUM(CASE WHEN action IN ('approved_send','draft_rejected') THEN 1 ELSE 0 END) AS total_all,
          SUM(CASE WHEN action='approved_send' THEN 1 ELSE 0 END) AS approved_all,
          SUM(CASE WHEN action='draft_rejected' THEN 1 ELSE 0 END) AS rejected_all,
          SUM(CASE WHEN action='approved_send'
                    AND json_extract(detail,'$.edited')=1 THEN 1 ELSE 0 END) AS edited_all,
          SUM(CASE WHEN action IN ('approved_send','draft_rejected') AND ts > ?
                   THEN 1 ELSE 0 END) AS total_30d,
          SUM(CASE WHEN action='approved_send' AND ts > ? THEN 1 ELSE 0 END) AS approved_30d,
          SUM(CASE WHEN action='draft_rejected' AND ts > ? THEN 1 ELSE 0 END) AS rejected_30d,
          SUM(CASE WHEN action='approved_send' AND ts > ?
                    AND json_extract(detail,'$.edited')=1 THEN 1 ELSE 0 END) AS edited_30d,
          SUM(CASE WHEN action='classified' AND (
                    json_extract(detail,'$.reasoning') LIKE 'noise%'
                    OR json_extract(detail,'$.reasoning') LIKE '%(fast path)%'
                    OR json_extract(detail,'$.fast_path')=1)
                   THEN 1 ELSE 0 END) AS fast_path,
          SUM(CASE WHEN action='classified' AND NOT (
                    json_extract(detail,'$.reasoning') LIKE 'noise%'
                    OR json_extract(detail,'$.reasoning') LIKE '%(fast path)%'
                    OR json_extract(detail,'$.fast_path')=1)
                   THEN 1 ELSE 0 END) AS llm_classified
        FROM audit_log
        WHERE action IN ('approved_send','draft_rejected','classified')
    """, (cutoff_30d, cutoff_30d, cutoff_30d, cutoff_30d))[0]

    def _make_stats(total, approved, rejected, edited):
        if not total:
            return {"total": 0, "approved": 0, "rejected": 0, "edited": 0,
                    "approval_rate": None, "edit_rate": None, "rejection_rate": None}
        return {
            "total": total, "approved": approved, "rejected": rejected, "edited": edited,
            "approval_rate": round(approved / total, 3),
            "edit_rate": round(edited / approved, 3) if approved else None,
            "rejection_rate": round(rejected / total, 3),
        }

    fast_path = agg["fast_path"] or 0
    llm_classified = agg["llm_classified"] or 0
    total_classified = fast_path + llm_classified
    result = {
        "all_time": _make_stats(
            agg["total_all"] or 0, agg["approved_all"] or 0,
            agg["rejected_all"] or 0, agg["edited_all"] or 0),
        "last_30d": _make_stats(
            agg["total_30d"] or 0, agg["approved_30d"] or 0,
            agg["rejected_30d"] or 0, agg["edited_30d"] or 0),
        "fast_path_rate": round(fast_path / total_classified, 3) if total_classified else None,
        "fast_path_count": fast_path,
        "llm_classified_count": llm_classified,
    }
    _quality_cache["data"] = result
    _quality_cache["ts"] = _tm.time()
    return result


_metrics_cache: dict = {"ts": 0.0, "data": None}
_METRICS_TTL = 60  # metrics accumulate slowly; 1-minute staleness is fine


@router.get("/metrics")
def system_metrics(refresh: bool = False):
    """Aggregated system improvement metrics across all subsystems.

    Surfaces: noise filtered, LLM calls avoided, sweep concurrency gains,
    draft quality, auto-retry success rate, loop closure rate. Designed to
    quantify cumulative improvement relative to a pre-improvement baseline.
    """
    import time
    if not refresh and _metrics_cache["data"] and time.time() - _metrics_cache["ts"] < _METRICS_TTL:
        return _metrics_cache["data"]
    # --- All audit_log counts in 1 query (11 COUNT queries → 1 table scan) ---
    agg = db.query("""
        SELECT
          SUM(CASE WHEN action='classified' THEN 1 ELSE 0 END) AS llm_classified,
          SUM(CASE WHEN action IN ('approved_send','draft_rejected') THEN 1 ELSE 0 END) AS drafts_total,
          SUM(CASE WHEN action='approved_send' THEN 1 ELSE 0 END) AS approved,
          SUM(CASE WHEN action='draft_rejected' THEN 1 ELSE 0 END) AS rejected,
          SUM(CASE WHEN action='draft_retried' THEN 1 ELSE 0 END) AS retried,
          SUM(CASE WHEN action='loop_opened' THEN 1 ELSE 0 END) AS loops_opened,
          SUM(CASE WHEN action='loop_closed' THEN 1 ELSE 0 END) AS loops_closed,
          SUM(CASE WHEN action='loop_run' THEN 1 ELSE 0 END) AS loops_run,
          SUM(CASE WHEN action='pr_reviewed' THEN 1 ELSE 0 END) AS prs_reviewed,
          SUM(CASE WHEN action='pr_reviewed'
               AND (json_extract(detail,'$.verdict')='approved'
                    OR (json_extract(detail,'$.verdict') IS NULL
                        AND CAST(json_extract(detail,'$.critical_count') AS INTEGER)=0))
               THEN 1 ELSE 0 END) AS prs_approved,
          SUM(CASE WHEN action IN ('auto_reply','holding_message','chieff_auto') THEN 1 ELSE 0 END) AS auto_sends
        FROM audit_log
    """)[0]

    llm_total_raw = agg["llm_classified"] or 0
    drafts_total = agg["drafts_total"] or 0
    approved = agg["approved"] or 0
    rejected = agg["rejected"] or 0
    retried = agg["retried"] or 0
    loops_opened = agg["loops_opened"] or 0
    loops_closed = agg["loops_closed"] or 0
    loops_run = agg["loops_run"] or 0
    prs_reviewed = agg["prs_reviewed"] or 0
    prs_approved = agg["prs_approved"] or 0
    auto_sends = agg["auto_sends"] or 0

    # Classification efficiency: items table (separate — different table).
    # Catch all fast-path reasoning patterns (they all contain "(fast path)").
    fast_path = db.query(
        "SELECT COUNT(*) AS n FROM items "
        "WHERE tier_reasoning LIKE '%(fast path)%' "
        "OR tier_reasoning LIKE 'noise%'")[0]["n"]
    llm_total = max(0, llm_total_raw - fast_path)
    total_items = fast_path + llm_total

    # Retry recovery requires a correlated subquery (stays separate).
    retry_succeeded = db.query(
        "SELECT COUNT(*) AS n FROM audit_log WHERE action='approved_send' "
        "AND json_extract(detail,'$.conversation_id') IN "
        "(SELECT json_extract(detail,'$.conversation_id') FROM audit_log "
        " WHERE action='draft_retried')")[0]["n"]

    # --- Sweep health (last 7 days from job_runs table) ---
    sweep_rows = db.query(
        "SELECT job, COUNT(*) AS runs, AVG(duration_ms) AS avg_ms "
        "FROM job_runs WHERE ok=1 AND started_at > ? "
        "GROUP BY job ORDER BY avg_ms DESC",
        (db.cutoff(days=7),))

    manual_sends = approved  # human-approved sends

    # --- Compute derived rates ---
    noise_rate = round(fast_path / total_items, 3) if total_items else None
    approval_rate = round(approved / drafts_total, 3) if drafts_total else None
    loop_closure_rate = round(loops_closed / loops_opened, 3) if loops_opened else None
    pr_approval_rate = round(prs_approved / prs_reviewed, 3) if prs_reviewed else None

    # --- Chief Ops Score (COS): product of independent improvement multipliers ---
    # Baseline (pre-loop, session start): error_rate=25%, backlog=63 items, reliability=75%,
    # VIP FYI precision=1% (essentially all forced to tier-C), LLM calls=100% of items.
    # Each multiplier = current_metric / baseline_metric; independent dimensions compound.
    import math

    # Dimension 1: Triage error rate reduction (infrastructure errors excluded)
    real_triage_errors = db.query(
        "SELECT COUNT(*) AS n FROM audit_log WHERE action='triage_error' "
        "AND ts > ? AND detail NOT LIKE '%fetch failed%' "
        "AND detail NOT LIKE '%session reset%'", (db.cutoff(days=7),))[0]["n"]
    sweep_runs_7d = db.query(
        "SELECT COUNT(*) AS n FROM job_runs WHERE job='teams_sweep' "
        "AND started_at > ?", (db.cutoff(days=7),))[0]["n"] or 1
    triage_error_rate = real_triage_errors / sweep_runs_7d
    triage_error_baseline = 0.25   # 25% batch error rate before improvements
    triage_x = min(100.0, triage_error_baseline / max(triage_error_rate, 0.001))

    # Dimension 2: Draft backlog quality (pending items — lower and more targeted is better)
    pending_drafts_now = db.query(
        "SELECT COUNT(*) AS n FROM drafts WHERE status='pending'")[0]["n"]
    backlog_baseline = 63   # items at session start
    backlog_x = min(100.0, backlog_baseline / max(pending_drafts_now, 1))

    # Dimension 3: VIP FYI precision (tier-C items correctly flagged as no-reply-needed),
    # measured against the first configured VIP sender.
    from .config import policy
    vip_sender = ((policy().get("vip") or [{}])[0].get("name") or "")
    vip_noreply_7d = db.query(
        "SELECT COUNT(*) AS n FROM audit_log WHERE action='classified' "
        "AND json_extract(detail,'$.needs_reply')=0 "
        "AND json_extract(detail,'$.tier') IN ('A','C') "
        "AND json_extract(detail,'$.sender')=? "
        "AND ts > ?", (vip_sender, db.cutoff(days=7)))[0]["n"]
    vip_total_7d = db.query(
        "SELECT COUNT(*) AS n FROM audit_log WHERE action='classified' "
        "AND json_extract(detail,'$.sender')=? "
        "AND ts > ?", (vip_sender, db.cutoff(days=7)))[0]["n"] or 1
    vip_precision = vip_noreply_7d / vip_total_7d
    vip_precision_baseline = 0.085   # 8.5% of VIP msgs correctly handled before (all forced to C)
    vip_x = min(100.0, vip_precision / max(vip_precision_baseline, 0.001))

    # Dimension 4: Job reliability (success rate of all scheduled jobs)
    job_stats = db.query(
        "SELECT SUM(ok) AS ok, COUNT(*) AS total FROM job_runs "
        "WHERE started_at > ?", (db.cutoff(days=1),))[0]
    reliability = (job_stats["ok"] or 0) / max(job_stats["total"] or 1, 1)
    reliability_baseline = 0.75   # 75% job success rate before fixes
    reliability_x = min(10.0, reliability / reliability_baseline)

    # Dimension 5: Draft approval quality (approved / decided; baseline ~1.5% of all drafts decided)
    drafts_approved_30d = db.query(
        "SELECT COUNT(*) AS n FROM drafts WHERE status='approved_sent' "
        "AND created_at > ?", (db.cutoff(days=30),))[0]["n"]
    drafts_decided_30d = db.query(
        "SELECT COUNT(*) AS n FROM drafts WHERE status IN ('approved_sent','rejected') "
        "AND created_at > ?", (db.cutoff(days=30),))[0]["n"]
    draft_approval_rate = drafts_approved_30d / max(drafts_decided_30d, 1)
    draft_approval_baseline = 0.015   # 1.5% of all drafts ever approved (pre-quality work)
    draft_quality_x = min(100.0, draft_approval_rate / draft_approval_baseline) if drafts_decided_30d >= 3 else 1.0

    # Composite: product of the three primary independent dimensions
    # (reliability is a small multiplier capped at 10; primary story is triage × backlog × vip)
    composite_x = round(triage_x * backlog_x * reliability_x, 1)
    # Full product including VIP precision (most dramatic improvement)
    full_product_x = round(triage_x * backlog_x * vip_x * reliability_x, 1)
    # Quality-augmented: composite × draft approval quality
    quality_x = round(composite_x * draft_quality_x, 1)

    result = {
        "chief_ops_score": {
            "composite_x": composite_x,
            "quality_x": quality_x,
            "full_product_x": full_product_x,
            "components": {
                "triage_error_reduction_x": round(triage_x, 1),
                "backlog_reduction_x": round(backlog_x, 1),
                "vip_precision_improvement_x": round(vip_x, 1),
                "draft_quality_improvement_x": round(draft_quality_x, 1),
                "reliability_improvement_x": round(reliability_x, 2),
            },
            "raw": {
                "triage_error_rate_7d": round(triage_error_rate, 4),
                "pending_drafts": pending_drafts_now,
                "vip_precision_7d": round(vip_precision, 3),
                "draft_approval_rate_30d": round(draft_approval_rate, 3),
                "reliability_24h": round(reliability, 4),
            },
            "baselines": {
                "triage_error_rate": triage_error_baseline,
                "backlog_items": backlog_baseline,
                "vip_precision": vip_precision_baseline,
                "draft_approval_rate": draft_approval_baseline,
                "reliability": reliability_baseline,
            },
        },
        "classification": {
            "total_items": total_items,
            "fast_path_count": fast_path,
            "llm_count": llm_total,
            "noise_rate": noise_rate,
            "llm_calls_avoided_pct": round(fast_path / total_items * 100, 1) if total_items else 0,
        },
        "drafts": {
            "total": drafts_total,
            "approved": approved,
            "rejected": rejected,
            "retried": retried,
            "retry_recovered": retry_succeeded,
            "approval_rate": approval_rate,
        },
        "loops": {
            "opened": loops_opened,
            "closed": loops_closed,
            "run_manually": loops_run,
            "closure_rate": loop_closure_rate,
        },
        "pr_review": {
            "reviewed": prs_reviewed,
            "approved": prs_approved,
            "approval_rate": pr_approval_rate,
        },
        "sends": {
            "auto": auto_sends,
            "manual": manual_sends,
            "automation_rate": round(auto_sends / (auto_sends + manual_sends), 3)
                               if (auto_sends + manual_sends) else None,
        },
        "sweep_perf_7d": [
            {"job": r["job"], "runs": r["runs"],
             "avg_ms": round(r["avg_ms"] or 0)}
            for r in sweep_rows if r["job"]
        ],
    }
    import time as _time
    _metrics_cache["data"] = result
    _metrics_cache["ts"] = _time.time()
    return result


_cost_cache: dict = {"ts": 0.0, "data": None}


@router.get("/cost")
def cost(refresh: bool = False):
    """Claude + Codex usage cost (today / month) from local CLI logs. Cached 5
    min; pass ?refresh=1 to force a fresh recompute (rescans the logs)."""
    import time
    from .cost import cos_summary, summary
    if refresh or _cost_cache["data"] is None or time.time() - _cost_cache["ts"] > 300:
        _cost_cache["data"] = summary()
        _cost_cache["ts"] = time.time()
    # CoS's own operating cost is metered live (cheap DB query), always fresh.
    return {**_cost_cache["data"], "cos": cos_summary()}


@router.get("/cost/cos")
def cost_cos_detail(month: str | None = None):
    """Detailed CoS cost breakdown for the Cost tab (daily / by purpose / by model).
    Pass ?month=YYYY-MM to view a past month; defaults to the current month."""
    from .cost import cos_detail
    return cos_detail(month)


@router.get("/cost/cos/report.pdf")
async def cost_cos_report_pdf(month: str | None = None):
    """One month's cost report as a printable PDF (headline stats, weekly/daily
    trend, by-purpose/by-model breakdown)."""
    from fastapi import Response
    from .cost import cos_detail, cos_report_pdf
    d = cos_detail(month)
    pdf = await cos_report_pdf(d)
    db.audit("cost_report_exported", {"month": d["month_key"], "format": "pdf"}, actor="user")
    return Response(content=pdf, media_type="application/pdf", headers={
        "Content-Disposition": f'inline; filename="cos-cost-report-{d["month_key"]}.pdf"'})


@router.get("/cost/cos/report.csv")
def cost_cos_report_csv(month: str | None = None):
    """One month's cost report as CSV (same breakdown as the PDF) — for
    spreadsheet import or as a plain-text receipt."""
    from fastapi import Response
    from .cost import cos_detail, cos_report_csv
    d = cos_detail(month)
    csv_text = cos_report_csv(d)
    db.audit("cost_report_exported", {"month": d["month_key"], "format": "csv"}, actor="user")
    return Response(content=csv_text, media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="cos-cost-report-{d["month_key"]}.csv"'})


_repos_cache: dict = {"ts": 0.0, "data": None}
_REPOS_TTL = 300  # repo list changes rarely; avoid `gh repo list` subprocess on every tab visit
_ado_cache: dict = {"ts": 0.0, "data": None}
_conf_cache: dict = {"ts": 0.0, "data": None}


@router.get("/ado-items")
def ado_items(refresh: bool = False):
    """The user's assigned Azure DevOps work items + states. Cached 5 min."""
    import time
    from .ado import my_items
    if refresh or _ado_cache["data"] is None or time.time() - _ado_cache["ts"] > 300:
        _ado_cache["data"] = my_items()
        _ado_cache["ts"] = time.time()
    return _ado_cache["data"]


@router.get("/ado-projects")
def ado_projects():
    """Which ADO project boards `/ado-items` polls. Toggle state lives in settings."""
    from .ado import configured_projects, disabled_projects
    disabled = disabled_projects()
    return [{"name": p, "enabled": p not in disabled} for p in configured_projects()]


class AdoProjectToggle(BaseModel):
    project: str
    enabled: bool


@router.post("/ado-projects")
def ado_projects_toggle(payload: AdoProjectToggle):
    import json as _json
    from .ado import disabled_projects
    disabled = disabled_projects()
    if payload.enabled:
        disabled.discard(payload.project)
    else:
        disabled.add(payload.project)
    db.set_setting("ado_projects_disabled", _json.dumps(sorted(disabled)))
    db.audit("ado_project_toggle", {"project": payload.project, "enabled": payload.enabled},
             actor="user")
    _ado_cache["data"] = None  # force refetch so the change takes effect immediately
    return {"ok": True, "disabled": sorted(disabled)}


@router.get("/confluence-updates")
def confluence_updates(refresh: bool = False):
    """Recent updates to Confluence pages the user follows or authored. Cached 5 min."""
    import time
    from .confluence import my_updates
    if refresh or _conf_cache["data"] is None or time.time() - _conf_cache["ts"] > 300:
        _conf_cache["data"] = my_updates()
        _conf_cache["ts"] = time.time()
    return _conf_cache["data"]


@router.get("/repos")
async def repos(refresh: bool = False):
    """Repos Chieff may review: org repos matching the configured prefixes,
    plus anything it has reviewed before. Toggle state lives in settings.
    `gh repo list` is cached 5 min — it's a slow subprocess and repos change rarely."""
    import asyncio as _asyncio
    import json as _json
    import re as _re
    import time as _time
    from .config import policy
    cfg = policy().get("pr_review", {})
    owner = cfg.get("github_owner") or policy().get("github", {}).get("owner", "your-github-org")
    prefixes = cfg.get("batch_repo_prefixes", ["arc-"])
    names: set[str] = set()

    if not refresh and _repos_cache["data"] and _time.time() - _repos_cache["ts"] < _REPOS_TTL:
        org_names = _repos_cache["data"]
    else:
        proc = await _asyncio.create_subprocess_exec(
            "gh", "repo", "list", owner, "--limit", "300", "--json", "name",
            stdout=_asyncio.subprocess.PIPE, stderr=_asyncio.subprocess.PIPE)
        out, _ = await proc.communicate()
        org_names = set()
        if proc.returncode == 0:
            org_names = {r["name"] for r in _json.loads(out.decode())
                         if any(r["name"].startswith(p) for p in prefixes)}
        _repos_cache["data"] = org_names
        _repos_cache["ts"] = _time.time()
    names |= org_names

    for row in db.query("SELECT DISTINCT external_id FROM items WHERE source='pr_review'"):
        m = _re.match(r"https://github\.com/[^/]+/([^/]+)/pull/", row["external_id"])
        if m:
            names.add(m.group(1))
    disabled = set(_json.loads(db.get_setting("pr_review_repos_disabled", "[]")))
    return [{"name": n, "enabled": n not in disabled} for n in sorted(names)]


class RepoToggle(BaseModel):
    repo: str
    enabled: bool


@router.post("/repos")
def repos_toggle(payload: RepoToggle):
    import json as _json
    disabled = set(_json.loads(db.get_setting("pr_review_repos_disabled", "[]")))
    if payload.enabled:
        disabled.discard(payload.repo)
    else:
        disabled.add(payload.repo)
    db.set_setting("pr_review_repos_disabled", _json.dumps(sorted(disabled)))
    db.audit("repo_review_toggle", {"repo": payload.repo, "enabled": payload.enabled},
             actor="user")
    return {"ok": True, "disabled": sorted(disabled)}


@router.get("/pr-approvals")
def pr_approvals():
    """Tracked PRs with derived status, plus the latest comments Chieff posted
    on each (for the expand view) and whether each is in the automation queue."""
    import json as _json
    disabled = set(_json.loads(db.get_setting("pr_queue_disabled", "[]")))
    rows = db.query("SELECT content FROM items WHERE source='pr_approval' "
                    "AND status != 'done' ORDER BY updated_at DESC")
    prs, all_urls = [], []
    for r in rows:
        try:
            pr = _json.loads(r["content"])
            prs.append(pr)
            all_urls.append(pr["url"].lower())
        except (TypeError, ValueError, KeyError):
            continue

    # Batch-fetch all PR comments in 1 query (was N queries, one per PR).
    comments_by_url: dict[str, list] = {u: [] for u in all_urls}
    if all_urls:
        ph = ",".join("?" * len(all_urls))
        comment_rows = db.query(
            f"SELECT json_extract(detail,'$.pr_url') AS url, ts, "
            f"       json_extract(detail,'$.body') AS body "
            f"FROM audit_log WHERE action='github_comment' "
            f"AND json_extract(detail,'$.pr_url') IN ({ph}) "
            f"ORDER BY id DESC",
            tuple(all_urls))
        seen: dict[str, int] = {}
        for c in comment_rows:
            if not c["body"] or not c["url"]:
                continue
            u = c["url"].lower()
            if seen.get(u, 0) < 5:
                comments_by_url.setdefault(u, []).append({"ts": c["ts"], "body": c["body"]})
                seen[u] = seen.get(u, 0) + 1

    out = []
    for pr in prs:
        url = pr["url"].lower()
        pr["in_queue"] = url not in disabled
        pr["comments"] = comments_by_url.get(url, [])
        out.append(pr)

    # surface PRs awaiting the user and changes-requested first, then by recency
    tone_rank = {"mine": 0, "warn": 1, "wait": 2, "good": 3, "muted": 4}
    out.sort(key=lambda p: (tone_rank.get(p.get("tone"), 5), p.get("updated_at") or ""),
             reverse=False)
    return out


class PrQueueToggle(BaseModel):
    url: str
    in_queue: bool


@router.post("/pr-approvals/queue")
def pr_queue_toggle(payload: PrQueueToggle):
    import json as _json
    url = payload.url.lower()
    disabled = set(_json.loads(db.get_setting("pr_queue_disabled", "[]")))
    if payload.in_queue:
        disabled.discard(url)
    else:
        disabled.add(url)
    db.set_setting("pr_queue_disabled", _json.dumps(sorted(disabled)))
    db.audit("pr_queue_toggle", {"url": url, "in_queue": payload.in_queue}, actor="user")
    return {"ok": True, "in_queue": payload.in_queue}


async def _pr_status():
    from .sweeps.pr_status import sweep
    return await sweep()


async def _pr_digest():
    from .sweeps.github_digest import digest
    return await digest()


async def _knowledge_sync():
    from .sweeps.knowledge import sync
    return await sync()


async def _weekly_status_generate():
    from .sweeps.weekly_status import generate
    return await generate()


async def _scrum_generate():
    from .sweeps.scrum import generate
    return await generate()


async def _pr_readiness_sweep():
    from .sweeps.pr_readiness import sweep
    return await sweep()


async def _pre_meeting_brief():
    from .sweeps.pre_meeting import sweep
    return await sweep()


async def _post_meeting_catchup():
    from .sweeps.post_meeting import sweep
    return await sweep()


# ---- Roadmap (preview / HITL edit) ----------------------------------------

@router.get("/roadmap")
def get_roadmap():
    """Stored roadmap JSON, or the seed if none saved yet."""
    import json as _json
    from .roadmap import SEED, SETTING_KEY
    raw = db.get_setting(SETTING_KEY)
    if not raw:
        return {"roadmap": SEED, "saved": False}
    try:
        return {"roadmap": _json.loads(raw), "saved": True}
    except Exception:
        return {"roadmap": SEED, "saved": False}


class RoadmapBody(BaseModel):
    roadmap: dict


@router.get("/roadmap/pdf")
async def roadmap_pdf():
    """Export the current roadmap as a landscape PDF (design-blueprint style)."""
    import json as _json
    from fastapi import Response
    from .roadmap import SEED, SETTING_KEY, to_pdf
    raw = db.get_setting(SETTING_KEY)
    rm = _json.loads(raw) if raw else SEED
    pdf = await to_pdf(rm)
    db.audit("roadmap_pdf_exported", {"title": rm.get("title")}, actor="user")
    return Response(content=pdf, media_type="application/pdf", headers={
        "Content-Disposition": 'inline; filename="os-conversions-timeline.pdf"'})


@router.get("/scorecard/pdf")
async def scorecard_pdf():
    """Export the delivery scorecard as a landscape PDF."""
    import json as _json
    from fastapi import Response
    from .roadmap import SEED, SETTING_KEY, scorecard_to_pdf
    raw = db.get_setting(SETTING_KEY)
    rm = _json.loads(raw) if raw else SEED
    if not rm.get("scorecard"):
        raise HTTPException(404, "no scorecard to export")
    pdf = await scorecard_to_pdf(rm)
    db.audit("scorecard_pdf_exported", {"title": rm["scorecard"].get("title")}, actor="user")
    return Response(content=pdf, media_type="application/pdf", headers={
        "Content-Disposition": 'inline; filename="delivery-scorecard.pdf"'})


@router.post("/roadmap/generate")
async def generate_roadmap():
    """Draft/refresh the roadmap from available data (PRs + ADO + notes)."""
    from .roadmap import draft
    return await draft()


@router.post("/roadmap")
def save_roadmap(body: RoadmapBody):
    """Persist HITL edits to the roadmap."""
    import json as _json
    from .roadmap import SETTING_KEY
    db.set_setting(SETTING_KEY, _json.dumps(body.roadmap))
    db.audit("roadmap_saved", {"lanes": sum(len(s.get("lanes", []))
             for s in body.roadmap.get("sections", []))}, actor="user")
    return {"ok": True}


# ---- Weekly status report -------------------------------------------------

@router.get("/weekly-status")
def weekly_status_list(limit: int = 8):
    """Recent weekly reports, newest first; the latest draft drives the tab."""
    rows = db.query(
        "SELECT id, week_key, week_label, date_range, title, confluence_page_id, "
        "confluence_url, edit_url, teams_html, metrics, status, created_at, updated_at "
        "FROM weekly_reports ORDER BY week_key DESC LIMIT ?", (limit,))
    import json as _json
    for r in rows:
        try:
            r["metrics"] = _json.loads(r.get("metrics") or "{}")
        except Exception:
            r["metrics"] = {}
    return rows


@router.get("/weekly-status/{report_id}/preview")
def weekly_status_preview(report_id: int):
    rows = db.query("SELECT storage_html, teams_html, title FROM weekly_reports WHERE id=?",
                    (report_id,))
    if not rows:
        raise HTTPException(404, "report not found")
    return rows[0]


@router.post("/weekly-status/generate")
async def weekly_status_generate():
    from .sweeps.weekly_status import generate
    return await generate()


@router.post("/weekly-status/{report_id}/approve")
async def approve_weekly_status(report_id: int):
    """Publish the Confluence draft live and post the summary to the configured
    Teams chat. Both honor the kill switch; the Teams post also honors dry-run."""
    from . import actions, confluence, graph, vault
    from .config import policy
    rows = db.query("SELECT * FROM weekly_reports WHERE id=?", (report_id,))
    if not rows:
        raise HTTPException(404, "report not found")
    rpt = rows[0]
    if rpt["status"] == "published":
        raise HTTPException(409, "already published")
    cfg = policy().get("weekly_status", {})

    # 1. publish to Confluence. Publish the existing draft, or (if the draft
    # could not be created earlier) create it published in one step.
    try:
        if rpt["confluence_page_id"]:
            published = confluence.publish_page(rpt["confluence_page_id"])
        else:
            published = confluence.create_page(
                cfg["space"], cfg["parent_page_id"], rpt["title"],
                rpt["storage_html"], draft=False)
    except confluence.SendBlocked as e:
        raise HTTPException(423, f"blocked: {e}")
    except Exception as e:
        raise HTTPException(
            502, f"Confluence publish failed (token may lack write access): {str(e)[:200]}")
    if published.get("status") == "dry_run":
        # Don't mark the report published — nothing actually went out, and doing
        # so would permanently block a real publish later (see the status=='published'
        # guard above) for a run that had no effect.
        return {"ok": True, "dry_run": True,
                "note": "dry run is on — Confluence publish and Teams post were skipped"}

    # 2. post the summary to the UseCases Tech Lead chat (guarded send path)
    topic = cfg.get("chat_topic")
    post_result = "skipped: no weekly_status.chat_topic configured"
    if topic:
        chat_id = None
        for c in await graph.chats_cached():
            if (c.get("topic") or "").strip().lower() == topic.strip().lower():
                chat_id = c["id"]
                break
        if not chat_id:
            post_result = f"chat '{topic}' not found"
        else:
            try:
                post_result = await actions.weekly_status_post(
                    chat_id, rpt["teams_html"], item_id=None)
            except actions.SendBlocked as e:
                post_result = f"blocked: {e}"

    db.execute("UPDATE weekly_reports SET status='published', confluence_url=?, "
               "updated_at=? WHERE id=?", (published["url"], db.now(), report_id))
    db.audit("weekly_status_published",
             {"week": rpt["week_key"], "url": published["url"], "teams_post": post_result},
             item_id=None, actor="user")
    vault.chieff_trace("Confluence", f"weekly status published: {rpt['title']} "
                                     f"-> {published['url']} (teams: {post_result})")
    return {"ok": True, "confluence_url": published["url"], "teams_post": post_result}


@router.post("/weekly-status/{report_id}/discard")
def discard_weekly_status(report_id: int):
    rows = db.query("SELECT week_key, status FROM weekly_reports WHERE id=?", (report_id,))
    if not rows:
        raise HTTPException(404, "report not found")
    db.execute("UPDATE weekly_reports SET status='discarded', updated_at=? WHERE id=?",
               (db.now(), report_id))
    db.audit("weekly_status_discarded", {"week": rows[0]["week_key"]}, actor="user")
    return {"ok": True}



# ---- Daily scrum report ----------------------------------------------------

@router.get("/scrum")
def scrum_list(limit: int = 10):
    """Recent scrum tickets, newest first; the latest drives the tab."""
    return db.query(
        "SELECT id, report_date, window_start, window_end, summary_text, "
        "generated_text, edited, created_at, updated_at "
        "FROM scrum_reports ORDER BY report_date DESC LIMIT ?", (limit,))


@router.post("/scrum/generate")
async def scrum_generate():
    from .sweeps.scrum import generate
    return await generate()


class ScrumEditBody(BaseModel):
    summary_text: str


@router.post("/scrum/{report_id}")
def scrum_edit(report_id: int, body: ScrumEditBody):
    """Save the user's hand-edit. Marks edited=1 so a later routine regenerate
    never overwrites it (see sweeps/scrum.py generate())."""
    rows = db.query("SELECT id FROM scrum_reports WHERE id=?", (report_id,))
    if not rows:
        raise HTTPException(404, "report not found")
    db.execute("UPDATE scrum_reports SET summary_text=?, edited=1, updated_at=? WHERE id=?",
              (body.summary_text, db.now(), report_id))
    db.audit("scrum_edited", {"report_id": report_id}, actor="user")
    return {"ok": True}


@router.post("/scrum/{report_id}/reset")
def scrum_reset(report_id: int):
    """Discard the hand-edit and go back to the last LLM-generated text."""
    rows = db.query("SELECT generated_text FROM scrum_reports WHERE id=?", (report_id,))
    if not rows:
        raise HTTPException(404, "report not found")
    db.execute("UPDATE scrum_reports SET summary_text=?, edited=0, updated_at=? WHERE id=?",
              (rows[0]["generated_text"], db.now(), report_id))
    db.audit("scrum_reset", {"report_id": report_id}, actor="user")
    return {"ok": True}


@router.post("/sweep/{name}")
async def run_sweep(name: str):
    from .daily import backfill_week, evening_shutdown, morning_brief
    from .sweeps import chieff, email, open_loops, pr_channel_review, pr_review, teams, transcripts
    sweeps = {
        "teams": teams.sweep,
        "chieff": chieff.handle_mentions,
        "chieff_daily_update": chieff.daily_update,
        "knowledge": _knowledge_sync,
        "pr_review_recent": lambda: pr_review.review_recent(days=2),
        "pr_review_orphans": pr_review.requeue_orphans,
        "pr_channel_review": pr_channel_review.sweep,
        "pr_readiness": _pr_readiness_sweep,
        "weekly_status": _weekly_status_generate,
        "scrum": _scrum_generate,
        "pr_status": _pr_status,
        "email": email.sweep,
        "open_loops": open_loops.sweep,
        "meeting_summaries": transcripts.sweep,
        "pr_review": pr_review.sweep,
        "pr_digest": _pr_digest,
        "morning_brief": morning_brief,
        "evening_shutdown": evening_shutdown,
        "backfill_week": backfill_week,
        "pre_meeting": _pre_meeting_brief,
        "post_meeting": _post_meeting_catchup,
    }
    from .hygiene import sweep as hygiene_sweep
    sweeps["hygiene"] = hygiene_sweep
    from .swarm import run_swarm
    sweeps["swarm"] = lambda: run_swarm(trigger="manual")
    if name not in sweeps:
        raise HTTPException(404, f"known sweeps: {sorted(sweeps)}")
    # record under the scheduler's job id so manual + scheduled runs share history
    aliases = {"teams": "teams_sweep", "email": "email_sweep",
               "meeting_summaries": "transcripts", "knowledge": "knowledge_sync"}
    from .health import tracked
    return await tracked(aliases.get(name, name), sweeps[name], trigger="manual")


# ---- Agent swarm --------------------------------------------------------------

@router.post("/swarm/run")
async def swarm_run(body: dict | None = None):
    """Fan out swarm workers now. Body: { profile?: board|ticket|pr_review|
    tech_debt, item_ids?: [int] }. item_ids targets specific ADO items;
    otherwise candidates come from policy swarm:. Returns the run id
    immediately; progress streams over /api/stream."""
    from . import swarm
    active = db.query(
        "SELECT id FROM swarm_runs WHERE status='running' AND started_at>? LIMIT 1",
        (db.cutoff(minutes=30),))
    if active:
        raise HTTPException(409, f"swarm run #{active[0]['id']} is still active")
    profile = (body or {}).get("profile") or "board"
    if profile not in ("board", "ado_scan", "ticket", "pr_scan", "pr_review",
                       "tech_debt", "thread"):
        raise HTTPException(400, f"unknown profile {profile}")
    ids = (body or {}).get("item_ids") or None
    prs = [{"repo": str(p["repo"]), "number": int(p["number"])}
           for p in (body or {}).get("prs") or [] if p.get("repo") and p.get("number")] or None
    conc = (body or {}).get("concurrency")
    conc = max(1, min(40, int(conc))) if conc else None
    run_id = swarm.start_run(trigger="manual", item_ids=ids, profile=profile,
                             pr_refs=prs, concurrency=conc)
    return {"ok": True, "run_id": run_id, "profile": profile}


_pr_cand_cache: dict = {}  # key ('' or repo) -> (monotonic ts, prs)
_pr_repos_cache: dict = {"ts": 0.0, "repos": None}


@router.get("/swarm/pr-candidates")
async def swarm_pr_candidates(refresh: bool = False, repo: str = ""):
    """PR picker candidates: involving the user by default, or ALL open PRs of one
    repo when ?repo= is set. Also returns the org repo list for the Repo
    filter. Cached 5 min (repos 1 h)."""
    import time as _time
    from . import swarm
    now = _time.monotonic()
    if refresh or _pr_repos_cache["repos"] is None or now - _pr_repos_cache["ts"] > 3600:
        try:
            _pr_repos_cache.update(ts=now, repos=await swarm.org_repos())
        except Exception:
            _pr_repos_cache.setdefault("repos", [])
    hit = _pr_cand_cache.get(repo)
    if not refresh and hit and now - hit[0] < 300:
        return {"prs": hit[1], "repos": _pr_repos_cache["repos"], "cached": True}
    prs = await swarm.pr_candidates_list(repo or None)
    _pr_cand_cache[repo] = (now, prs)
    return {"prs": prs, "repos": _pr_repos_cache["repos"], "cached": False}


_swarm_candidates_cache: dict = {"ts": 0.0, "items": None}


@router.get("/swarm/candidates")
def swarm_candidates(refresh: bool = False):
    """Recently-changed items across the swarm's projects (everyone's, 30 days)
    for the target picker: the UI filters by owner/type/state client-side and
    multi-selects items to assign to a run. Cached 5 min."""
    import time as _time
    from . import ado
    from .config import policy as _policy
    now = _time.monotonic()
    if (not refresh and _swarm_candidates_cache["items"] is not None
            and now - _swarm_candidates_cache["ts"] < 300):
        return {"items": _swarm_candidates_cache["items"], "cached": True}
    projects = _policy().get("swarm", {}).get("projects") or None
    items = ado.project_items(projects)
    _swarm_candidates_cache.update(ts=now, items=items)
    return {"items": items, "cached": False}


@router.post("/swarm/runs/{run_id}/stop")
def swarm_stop(run_id: int):
    """HITL intervention: cancel a running swarm mid-flight."""
    from . import swarm
    if not swarm.stop_run(run_id):
        raise HTTPException(404, "run is not active (or was started outside the board)")
    return {"ok": True}


@router.post("/swarm/runs/{run_id}/control")
def swarm_run_control(run_id: int, body: dict):
    """Pause / resume / stop — the whole run (no target) or one ticket/PR
    (target like 'ado:12345'). Pause takes effect between workers."""
    from . import swarm
    action = (body or {}).get("action")
    target = (body or {}).get("target") or None
    if action not in ("pause", "resume", "stop"):
        raise HTTPException(400, "action must be pause | resume | stop")
    return swarm.control(run_id, action, target)


# ---- swarm worktrees (HITL approve/discard) ------------------------------------

@router.get("/worktrees")
def worktrees_list(limit: int = 50):
    from . import worktrees
    return {"worktrees": worktrees.list_worktrees(limit),
            "ttl_hours": worktrees.ttl_hours()}


@router.post("/worktrees/{wt_id}/approve")
async def worktree_approve(wt_id: int):
    """Push the branch + open a draft PR (kill switch + dry run apply)."""
    from . import actions, worktrees
    rows = db.query("SELECT * FROM swarm_worktrees WHERE id=?", (wt_id,))
    if not rows:
        raise HTTPException(404, "worktree not found")
    if rows[0]["status"] != "ready":
        raise HTTPException(409, f"worktree is {rows[0]['status']}, not ready")
    try:
        result = await actions.swarm_pr_open(rows[0])
    except actions.SendBlocked as e:
        raise HTTPException(423, f"blocked: {e}")
    from .events import broadcast
    broadcast("worktree_update", {"worktree_id": wt_id})
    return result


@router.post("/worktrees/{wt_id}/discard")
async def worktree_discard(wt_id: int):
    from . import worktrees
    try:
        return await worktrees.discard(wt_id, reason="discarded by the user on the board")
    except ValueError:
        raise HTTPException(404, "worktree not found")


@router.get("/swarm/runs")
def swarm_runs(limit: int = 20, profiles: str = ""):
    from . import swarm
    plist = [p.strip() for p in profiles.split(",") if p.strip()] or None
    return {"runs": swarm.list_runs(limit, plist)}


@router.get("/swarm/runs/{run_id}")
def swarm_run_detail(run_id: int):
    from . import swarm
    result = swarm.get_run(run_id)
    if not result:
        raise HTTPException(404, "run not found")
    return result


@router.get("/swarm/latest")
def swarm_latest(profiles: str = ""):
    from . import swarm
    plist = [p.strip() for p in profiles.split(",") if p.strip()] or None
    result = swarm.latest(plist) or {"run": None, "jobs": []}
    if result.get("run") and result["run"]["status"] == "running":
        result["control"] = swarm.control_state(result["run"]["id"])
    return result


@router.get("/swarm/activity")
def swarm_activity(limit: int = 100, profiles: str = ""):
    """Receipts log: every swarm action from the audit trail, newest first."""
    from . import swarm
    plist = [p.strip() for p in profiles.split(",") if p.strip()] or None
    return {"activity": swarm.activity(limit, plist)}


@router.get("/swarm/target")
def swarm_target(kind: str, id: int):
    """Full swarm history for one ticket/PR (the board's click inspector)."""
    from . import swarm
    if kind not in ("ado", "pr"):
        raise HTTPException(400, "kind must be ado or pr")
    return swarm.target_history(kind, id)


@router.get("/swarm/board")
def swarm_board(profiles: str = "", span: int = 10):
    """Persistent board: last N runs merged, newest state per target."""
    from . import swarm
    plist = [p.strip() for p in profiles.split(",") if p.strip()] or None
    return swarm.board_state(plist, min(span, 30))


@router.get("/swarm/ticket-template")
def swarm_ticket_template():
    """The swarm-ready ticket template (runbooks/_ticket-template.md)."""
    from .config import REPO_ROOT
    f = REPO_ROOT / "runbooks" / "_ticket-template.md"
    return {"template": f.read_text() if f.exists() else ""}


@router.get("/swarm/debt")
def swarm_debt():
    """Unaddressed tech-debt findings collected by pr_review runs, grouped by
    repo — the Debt Swarm's backlog."""
    from . import swarm
    return {"debt": swarm._unaddressed_debt(cap_per_repo=10)}


@router.post("/swarm/retry")
async def swarm_retry(payload: dict):
    """Retry one ticket from a chosen pipeline stage (board retry dropdown)."""
    from . import swarm
    try:
        item_id = int(payload.get("id"))
        stage = str(payload.get("stage") or "")
    except (TypeError, ValueError):
        raise HTTPException(400, f"bad payload: {payload}")
    return await swarm.retry_stage(item_id, stage)


@router.post("/companion")
async def companion_say(payload: dict):
    """In-app companion: synchronous single-turn for the web UI (no Teams, no
    polling). Sub-second for commands. Body: {text}."""
    from . import companion
    text = str((payload or {}).get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text is required")
    return await companion.answer(text, history=(payload or {}).get("history"))


@router.post("/companion/proactive")
async def companion_proactive_now():
    """Run the proactive check now (brief-me / test trigger)."""
    from . import companion
    return await companion.proactive_sweep()


@router.get("/companion/providers")
def companion_providers():
    """Provider + model catalog for the CoS panel's picker, plus what's
    currently selected. `available` reflects whether the CLI is actually on
    PATH, so the UI can grey out a provider instead of failing on send."""
    from . import providers
    prov, model = providers.selection()
    return {"providers": providers.catalog(), "provider": prov, "model": model}


class CompanionProviderBody(BaseModel):
    provider: str
    model: str


@router.post("/companion/providers")
def companion_set_provider(body: CompanionProviderBody):
    from . import providers
    try:
        prov, model = providers.set_selection(body.provider, body.model)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "provider": prov, "model": model}


@router.get("/companion/memory")
def companion_memory_list(q: str = ""):
    """What the companion remembers (the panel's Memory view)."""
    from . import companion_memory as mem
    mem.seed_defaults()
    return {"memory": mem.recall(q, limit=100) if q else mem.all_memories()}


@router.post("/companion/memory")
def companion_memory_add(payload: dict):
    from . import companion_memory as mem
    text = str((payload or {}).get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text is required")
    return {"id": mem.add(text, kind=str((payload or {}).get("kind") or "fact"),
                          topic=str((payload or {}).get("topic") or ""),
                          pinned=bool((payload or {}).get("pinned")), source="user")}


@router.delete("/companion/memory")
def companion_memory_forget(q: str):
    from . import companion_memory as mem
    return {"removed": mem.forget(q)}


@router.get("/watchers")
async def watchers_list():
    """All watchers with their recent events (the Watchers panel)."""
    from . import watchers
    watchers.seed_defaults()
    return {"watchers": await watchers.list_watchers(),
            "default_prompt": watchers.WATCHER_AGENT.system_prompt}


@router.get("/watchers/chats")
async def watchers_chats():
    """Chats to pick a watcher target from, most-active first (the cache is
    ordered by last activity). Searchable client-side."""
    from . import graph
    chats = await graph.chats_cached()
    return {"chats": [{
        "id": c["id"],
        "topic": (c.get("topic") or "").strip(),
        "type": c.get("chatType") or "",
    } for c in chats]}


@router.post("/watchers")
async def watchers_create(payload: dict):
    from . import watchers
    if not str(payload.get("target") or "").strip():
        raise HTTPException(400, "target is required")
    try:
        wid = await watchers.create_watcher(
            str(payload.get("name") or "").strip(),
            str(payload.get("kind") or "teams_chat"),
            str(payload.get("target") or "").strip(),
            payload.get("guardrails"),
            int(payload.get("interval_minutes") or 5))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "id": wid}


@router.post("/watchers/{wid}")
def watchers_update(wid: int, payload: dict):
    """Toggle / edit guardrails / rename — the UI's inline edits."""
    from . import watchers
    watchers.update_watcher(wid, payload or {})
    return {"ok": True}


@router.delete("/watchers/{wid}")
def watchers_delete(wid: int):
    from . import watchers
    watchers.delete_watcher(wid)
    return {"ok": True}


@router.post("/watchers/{wid}/run")
async def watchers_run(wid: int):
    """Run one watcher now, regardless of its interval."""
    from . import watchers
    return {"ok": True, "result": await watchers.sweep(force_id=wid)}


@router.get("/swarm/gates")
def swarm_gates():
    """Loop counts for the gates between board lanes (effective values)."""
    from . import gates
    return {"gates": gates.gates_state(), "max": gates.MAX_LOOPS}


@router.post("/swarm/gates")
def swarm_gates_set(payload: dict):
    """Set a gate's loop count from the board (clamped 0..5)."""
    from . import gates
    try:
        state = gates.set_gate(str(payload.get("gate")), payload.get("loops"))
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, f"unknown gate or bad loop count: {payload}")
    return {"gates": state, "max": gates.MAX_LOOPS}


# ---- Obsidian vault endpoints -----------------------------------------------

@router.get("/obsidian/stats")
def obsidian_stats():
    from .obsidian import vault_stats
    return vault_stats()


@router.get("/obsidian/recent")
def obsidian_recent(limit: int = 30, q: str = ""):
    from .obsidian import recent_notes
    notes = recent_notes(min(limit, 200))
    if q:
        needle = q.lower()
        notes = [
            n for n in notes
            if needle in n.get("name", "").lower() or needle in (n.get("preview") or "").lower()
        ][:min(limit, 50)]
    return notes


@router.get("/obsidian/graph")
def obsidian_graph(max_nodes: int = 120):
    from .obsidian import note_graph
    return note_graph(min(max_nodes, 300))


@router.get("/obsidian/note")
def obsidian_note(path: str):
    from .obsidian import read_note
    result = read_note(path)
    if result is None:
        raise HTTPException(404, "note not found")
    return result


@router.post("/obsidian/note")
def obsidian_create_note(body: dict):
    """Create or append to a note in the Obsidian vault.
    Body: { title: str, content: str, folder: str = "", append: bool = False }
    Returns: { path: str, created: bool }
    """
    from .obsidian import create_note
    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "title required")
    content = body.get("content", "")
    folder = body.get("folder", "")
    append = bool(body.get("append", False))
    result = create_note(title, content, folder=folder, append=append)
    if result is None:
        raise HTTPException(500, "could not write note")
    return result


@router.get("/obsidian/activity")
def obsidian_activity(days: int = 7):
    """Notes modified per day for the last N days."""
    from .obsidian import vault_activity
    return vault_activity(min(days, 30))


@router.get("/obsidian/search")
def obsidian_search(q: str, limit: int = 20):
    """Full-text grep through Obsidian vault markdown files."""
    from .obsidian import search_vault
    if not q or len(q.strip()) < 2:
        raise HTTPException(400, "q must be at least 2 chars")
    return search_vault(q.strip(), min(limit, 50))


# ---- Obsidian board capture --------------------------------------------------

@router.post("/obsidian/capture-board")
async def obsidian_capture_board():
    """Snapshot current board state (pings, loops, drafts, focus) into an Obsidian note."""
    import datetime
    from .obsidian import create_note

    now = datetime.datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M")

    active_pings = db.query(
        "SELECT sender, content, tier, urgent FROM items "
        "WHERE type='ping' AND status NOT IN ('dismissed','done') "
        "ORDER BY urgent DESC, received_at DESC LIMIT 10")
    open_loops = db.query(
        "SELECT subject, content FROM items "
        "WHERE type='open_loop' AND status NOT IN ('done') LIMIT 10")
    pending_drafts = db.query(
        "SELECT subject, recipient FROM items "
        "WHERE type='draft' AND status='pending' LIMIT 5")

    lines = [
        f"# CoS Board Snapshot — {date_str} {time_str}",
        "",
        f"## Active Pings ({len(active_pings)})",
    ]
    for p in active_pings:
        urgent = " 🔴" if p.get("urgent") else ""
        lines.append(f"- **[T{p.get('tier','?')}]** {p.get('sender','?')}{urgent}: {(p.get('content') or '')[:120]}")

    lines += ["", f"## Open Loops ({len(open_loops)})"]
    for l in open_loops:
        lines.append(f"- [{(l.get('subject') or 'loop').replace('_',' ')}] {(l.get('content') or '')[:120]}")

    if pending_drafts:
        lines += ["", f"## Pending Drafts ({len(pending_drafts)})"]
        for d in pending_drafts:
            lines.append(f"- {d.get('subject','?')} → {d.get('recipient','?')}")

    lines += ["", "---", f"*Captured from Chief of Staff at {time_str}*"]
    content = "\n".join(lines)
    title = f"CoS-{date_str}-{time_str.replace(':','-')}"
    result = create_note(title, content, folder="Chieff")
    if not result:
        raise HTTPException(500, "could not write capture note")
    db.audit("obsidian_capture", {"path": result["path"]}, actor="user")
    return {"ok": True, "path": result["path"], "title": title}


# ---- Obsidian daily note -----------------------------------------------------

@router.get("/obsidian/daily")
def obsidian_daily(date: str = ""):
    """Return the daily note for the given date (YYYY-MM-DD), defaulting to today."""
    from .obsidian import read_note, vault_root
    from pathlib import Path
    import datetime
    if not date:
        date = datetime.date.today().isoformat()
    root = vault_root()
    # Daily notes are stored as <date>.md at the vault root or in a Dailies subfolder
    candidates = [
        f"{date}.md",
        f"Dailies/{date}.md",
        f"Daily/{date}.md",
        f"Daily Notes/{date}.md",
    ]
    for rel in candidates:
        note = read_note(rel)
        if note is not None:
            note["date"] = date
            return note
    return {"date": date, "found": False, "content": None, "path": None}


# ---- AI Platform knowledge hub -----------------------------------------------

@router.get("/knowledge/pages")
def knowledge_pages(use_case: str | None = None, doc_type: str | None = None,
                    space: str | None = None, q: str | None = None, limit: int = 100):
    clauses, params = [], []
    if use_case:
        clauses.append("use_case=?")
        params.append(use_case)
    if doc_type:
        clauses.append("doc_type=?")
        params.append(doc_type)
    if space:
        clauses.append("space=?")
        params.append(space)
    if q:
        clauses.append("(title LIKE ? OR content_md LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = db.query(
        f"SELECT id, title, url, space, use_case, doc_type, word_count, has_code, "
        f"last_edited_by, last_edited_at, linked_repos, parent_title "
        f"FROM knowledge_pages {where} ORDER BY last_edited_at DESC LIMIT ?",
        tuple(params) + (min(limit, 500),))
    return {"pages": rows, "total": len(rows)}


@router.get("/knowledge/stats")
def knowledge_stats():
    import json as _json
    import re as _re
    from pathlib import Path as _Path
    from .config import policy as _policy, vault_root as _vault_root

    by_uc = {r["use_case"] or "Other": r["n"] for r in
             db.query("SELECT use_case, COUNT(*) AS n FROM knowledge_pages GROUP BY use_case")}
    by_dt = {r["doc_type"] or "other": r["n"] for r in
             db.query("SELECT doc_type, COUNT(*) AS n FROM knowledge_pages GROUP BY doc_type")}
    agg = db.query("""
        SELECT COUNT(*) AS total, AVG(word_count) AS avg_words,
               SUM(has_code) AS with_code, SUM(has_diagrams) AS with_diagrams
        FROM knowledge_pages
    """)[0]
    cutoff_7d = db.cutoff(days=7)
    cutoff_30d = db.cutoff(days=30)
    stale = db.query("""
        SELECT
          SUM(CASE WHEN synced_at > ? THEN 1 ELSE 0 END) AS fresh_7d,
          SUM(CASE WHEN synced_at > ? AND synced_at <= ? THEN 1 ELSE 0 END) AS fresh_30d,
          SUM(CASE WHEN synced_at <= ? THEN 1 ELSE 0 END) AS stale_30d_plus
        FROM knowledge_pages
    """, (cutoff_7d, cutoff_7d, cutoff_30d, cutoff_30d))[0]
    authors = db.query("""
        SELECT last_edited_by AS name, COUNT(*) AS count
        FROM knowledge_pages WHERE last_edited_by IS NOT NULL AND last_edited_by != '?'
        GROUP BY last_edited_by ORDER BY count DESC LIMIT 10
    """)

    ado_stats: dict = {"total_items": 0, "by_state": {}, "by_type": {}, "active_epics": []}
    try:
        cfg = _policy().get("knowledge", {})
        ado_file = (_vault_root() / cfg.get("dir", "Chieff/Knowledge/AI Platform")
                    / "ADO" / "AI Platform.md")
        if ado_file.exists():
            text = ado_file.read_text()
            for state in ("Active", "Closed", "New", "Resolved"):
                ado_stats["by_state"][state] = len(_re.findall(rf"state: \*\*{state}\*\*", text))
            ado_stats["total_items"] = sum(ado_stats["by_state"].values())
            for display, key in (("Epic", "Epic"), ("Feature", "Feature"),
                                  ("Product Backlog Item", "PBI"), ("Bug", "Bug"), ("Task", "Task")):
                m = _re.search(rf"^## {_re.escape(display)}s \((\d+)\)", text, _re.MULTILINE)
                ado_stats["by_type"][key] = int(m.group(1)) if m else 0
            for m in _re.finditer(
                    r"### Epic (\d+): (.+?)\n- state: \*\*(\w[\w\s]*)\*\*([^\n]*)", text, _re.MULTILINE):
                if m.group(3) == "Active":
                    tags_m = _re.search(r"\| tags: ([^|]+)", m.group(4))
                    ado_stats["active_epics"].append({
                        "id": m.group(1), "title": m.group(2).strip(),
                        "tags": tags_m.group(1).strip() if tags_m else ""})
    except Exception:
        pass

    return {
        "total_pages": agg["total"] or 0,
        "by_use_case": by_uc,
        "by_doc_type": by_dt,
        "avg_word_count": round(agg["avg_words"] or 0),
        "pages_with_code": agg["with_code"] or 0,
        "pages_with_diagrams": agg["with_diagrams"] or 0,
        "staleness": {
            "fresh_7d": stale["fresh_7d"] or 0,
            "fresh_30d": stale["fresh_30d"] or 0,
            "stale_30d_plus": stale["stale_30d_plus"] or 0,
        },
        "top_authors": [{"name": r["name"], "count": r["count"]} for r in authors],
        "ado_stats": ado_stats,
    }


@router.get("/knowledge/doc-gaps")
def knowledge_doc_gaps():
    import json as _json
    import re as _re

    # Discover repos from PR history (GitHub URLs)
    known_repos: set[str] = set()
    for r in db.query("SELECT content FROM items WHERE source='pr_approval' AND content IS NOT NULL"):
        try:
            url = _json.loads(r["content"]).get("url", "")
            m = _re.match(r"https://github\.com/[^/]+/([^/]+)/pull/", url)
            if m:
                known_repos.add(m.group(1))
        except Exception:
            pass
    for r in db.query("SELECT DISTINCT external_id FROM items WHERE source='pr_review'"):
        m = _re.match(r"https://github\.com/[^/]+/([^/]+)/pull/", r["external_id"] or "")
        if m:
            known_repos.add(m.group(1))

    # All knowledge pages with metadata
    all_pages = db.query(
        "SELECT title, linked_repos, doc_type, use_case, content_md FROM knowledge_pages")

    # Build repo→pages from explicit linked_repos matches
    repo_pages: dict[str, list[dict]] = {}
    for row in all_pages:
        try:
            repos = _json.loads(row["linked_repos"] or "[]")
        except Exception:
            repos = []
        for repo in repos:
            repo_pages.setdefault(repo, []).append(
                {"title": row["title"], "doc_type": row["doc_type"], "match": "explicit"})

    # Use-case keyword → page use_case lookup
    uc_pages: dict[str, list[dict]] = {}
    for row in all_pages:
        uc = row.get("use_case") or "Other"
        uc_pages.setdefault(uc.lower(), []).append(
            {"title": row["title"], "doc_type": row["doc_type"], "match": "use-case"})

    # Keyword map: repo name fragment → use-case label. Shares the policy
    # knowledge.use_case_rules mapping used by the knowledge sweep's
    # _detect_use_case (defaults are generic examples).
    from .config import policy as _policy
    UC_KEYWORDS = (_policy().get("knowledge", {}).get("use_case_rules") or {
        "intake": "Intake", "onboarding": "Intake",
        "platform": "Platform", "idp": "Platform", "implementation": "Platform",
    })

    def _uc_for_repo(name: str) -> str | None:
        n = name.lower()
        for kw, uc in UC_KEYWORDS.items():
            if kw.lower() in n:
                return uc.lower()
        return None

    result = []
    for name in sorted(known_repos | set(repo_pages)):
        pages = list(repo_pages.get(name, []))
        # Fuzzy-add use-case pages if no explicit match
        if not pages:
            uc = _uc_for_repo(name)
            if uc:
                pages = uc_pages.get(uc, [])[:3]  # max 3 fuzzy matches

        n = len(pages)
        explicit = [p for p in pages if p.get("match") == "explicit"]
        fuzzy = [p for p in pages if p.get("match") != "explicit"]
        # Scoring: explicit matches count more than fuzzy
        score = (
            (100 if len(explicit) >= 2 else 70 if len(explicit) == 1 else 0) +
            (20 if fuzzy and not explicit else 0)
        )
        score = min(score, 100)
        has_doc = n > 0
        doc_types = {p["doc_type"] for p in pages}
        gaps = [g for g, dt in (("no architecture doc", "architecture"),
                                 ("no runbook", "runbook"), ("no guide", "guide"))
                if dt not in doc_types]
        match_type = ("explicit" if explicit else "use-case" if fuzzy else "none")
        result.append({"name": name, "has_doc": has_doc,
                        "pages": [p["title"] for p in pages],
                        "score": score, "gaps": gaps, "match_type": match_type})
    result.sort(key=lambda x: -x["score"])
    return {"repos": result}


@router.post("/knowledge/full-sync")
async def knowledge_full_sync():
    from .sweeps.knowledge import full_sync
    return await full_sync()


# ---- Agentic architecture map -----------------------------------------------

@router.get("/arch-map")
def arch_map():
    from .arch_map import get_map
    return get_map()


# ---- Deployed-agent live monitor (OSCA) -------------------------------------

@router.get("/osca/health")
async def osca_health(force: bool = False):
    """Live pipeline health: status, totals, stage/milestone/flow breakdown,
    throughput. Drives the flow-map overlay and the monitor header."""
    from . import osca
    return await osca.get_health(force=force)


@router.get("/osca/problems")
async def osca_problems(status: str = "open", force: bool = False):
    """Notification-center feed (problems grouped by signature). `status`:
       open   → active + acknowledged (persisted; ack survives refresh)
       active → live groups only (never acked)
       all    → include resolved (history)
    Persisted records (from the sweep) carry ack/resolve state; if the sweep
    hasn't run yet we fall back to the live grouped list."""
    from . import osca

    if status == "active":
        return {"problems": await osca.get_problems(force=force), "source": "live"}

    # persisted view (ack-aware)
    where = "WHERE status != 'resolved'" if status == "open" else ""
    rows = db.query(
        f"SELECT key, kind, severity, title, detail, entity, stage, reason, "
        f"count, recent_count, examples, first_seen, last_seen, source_ts, "
        f"status, acked_at, resolved_at "
        f"FROM osca_problems {where} "
        f"ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, "
        f"recent_count DESC, count DESC")
    if not rows:
        # sweep hasn't populated yet — serve the live grouped list
        return {"problems": await osca.get_problems(force=force), "source": "live"}
    for r in rows:
        try:
            r["examples"] = _json.loads(r["examples"]) if r.get("examples") else []
        except (ValueError, TypeError):
            r["examples"] = []
    return {"problems": rows, "source": "persisted"}


@router.get("/osca/problems/{key:path}/detail")
async def osca_problem_detail(key: str, force: bool = False):
    """Individual instances within a problem group (live drill-down)."""
    from . import osca
    return {"instances": await osca.get_problem_detail(key, force=force)}


@router.post("/osca/refresh")
async def osca_refresh():
    """Force a fresh poll + persist (runs the sweep on demand)."""
    from .sweeps.osca import sweep
    return await sweep()


class OscaAckBody(BaseModel):
    status: str = "acknowledged"  # acknowledged | active | resolved


@router.post("/osca/problems/{key:path}/ack")
def osca_ack(key: str, payload: OscaAckBody):
    """Acknowledge / re-open / resolve a single problem in the notification center."""
    allowed = {"acknowledged", "active", "resolved"}
    if payload.status not in allowed:
        raise HTTPException(400, f"settable: {sorted(allowed)}")
    exists = db.query("SELECT key FROM osca_problems WHERE key=?", (key,))
    if not exists:
        raise HTTPException(404, "no problem with that key")
    ts = db.now()
    col = {"acknowledged": "acked_at", "resolved": "resolved_at"}.get(payload.status)
    if col:
        db.execute(f"UPDATE osca_problems SET status=?, {col}=? WHERE key=?",
                   (payload.status, ts, key))
    else:
        db.execute("UPDATE osca_problems SET status='active', acked_at=NULL WHERE key=?", (key,))
    db.audit("osca_problem_ack", {"key": key, "status": payload.status})
    return {"ok": True, "key": key, "status": payload.status}


@router.get("/osca/logs")
async def osca_logs(workload: str, hours: int = 6, limit: int = 50):
    """Raw log lines for one workload — Mission Control alert drill-down."""
    from . import osca
    return await osca.get_workload_logs(workload, hours=hours, limit=limit)


@router.get("/osca/trend")
def osca_trend(limit: int = 48):
    """Recent health snapshots for the sparkline/trend view."""
    rows = db.query(
        "SELECT ts, status, active_clients, escalations, paused, blocked, orphaned, dead_letter "
        "FROM osca_snapshots ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),))
    return {"snapshots": list(reversed(rows))}
