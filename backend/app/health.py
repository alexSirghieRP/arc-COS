"""Agentic health: job-run tracking, backend log ring buffer, health summary.

Every sweep/scheduled job run lands in job_runs (start, duration, ok/error,
summary) whether it was fired by the scheduler or manually from the board.
/api/health aggregates that with live MCP session stats and agent-run health;
/api/logs serves the in-memory log tail so the board can show what the backend
is actually doing without ssh-ing into launchd logs.
"""

import collections
import json
import logging
import time
from datetime import datetime, timezone

from . import db

log = logging.getLogger("chief.health")

# ---- backend log ring buffer -----------------------------------------------

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


class RingBufferHandler(logging.Handler):
    """Keeps the last N log records in memory for the board's Logs tab."""

    def __init__(self, capacity: int = 4000):
        super().__init__(level=logging.DEBUG)
        self.records: collections.deque = collections.deque(maxlen=capacity)
        self._seq = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info and not record.exc_text:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            if record.exc_text:
                message = f"{message}\n{record.exc_text}"
            self._seq += 1
            self.records.append({
                "id": self._seq,
                "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
                "level": record.levelname,
                "levelno": record.levelno,
                "logger": record.name,
                "message": message[:4000],
            })
        except Exception:  # a broken log record must never take the app down
            pass


buffer_handler = RingBufferHandler()


def install_log_buffer() -> None:
    root = logging.getLogger()
    if buffer_handler not in root.handlers:
        root.addHandler(buffer_handler)


def tail_logs(level: str | None = None, q: str | None = None,
              limit: int = 300, after_id: int | None = None) -> dict:
    """Filtered view of the ring buffer, oldest-first. after_id enables cheap
    incremental polling from the UI (only records newer than the last seen)."""
    records = list(buffer_handler.records)
    minlevel = _LEVELS.get((level or "").upper(), 0)
    needle = (q or "").lower()
    rows = [r for r in records
            if r["levelno"] >= minlevel
            and (not after_id or r["id"] > after_id)
            and (not needle or needle in r["message"].lower() or needle in r["logger"].lower())]
    counts: dict[str, int] = {}
    for r in records:
        counts[r["level"]] = counts.get(r["level"], 0) + 1
    return {"logs": rows[-limit:], "counts": counts,
            "last_id": records[-1]["id"] if records else 0}


# ---- job-run tracking --------------------------------------------------------

_KEEP_RUNS = 5000  # global cap; pruned after each insert


async def tracked(job: str, fn, trigger: str = "schedule"):
    """Run one job while recording start/duration/outcome in job_runs.
    Re-raises the job's exception so callers keep their own error handling."""
    run_id = db.execute(
        "INSERT INTO job_runs(job, trigger, started_at) VALUES(?,?,?)",
        (job, trigger, db.now()))
    t0 = time.monotonic()
    try:
        from .events import broadcast as _bc
        _bc("sweep_started", {"job": job, "trigger": trigger})
    except Exception:
        pass
    try:
        result = await fn()
    except Exception as e:
        db.execute(
            "UPDATE job_runs SET finished_at=?, duration_ms=?, ok=0, error=? WHERE id=?",
            (db.now(), int((time.monotonic() - t0) * 1000), str(e)[:500], run_id))
        _prune()
        raise
    summary = None
    if isinstance(result, (dict, list)):
        summary = json.dumps(result, ensure_ascii=False, default=str)[:500]
    elif result is not None:
        summary = str(result)[:500]
    db.execute(
        "UPDATE job_runs SET finished_at=?, duration_ms=?, ok=1, summary=? WHERE id=?",
        (db.now(), int((time.monotonic() - t0) * 1000), summary, run_id))
    _prune()
    try:
        from .events import broadcast
        msg = _sweep_toast(job, result)
        extra = {"msg": msg} if msg else {}
        broadcast("board_changed", {"job": job, "trigger": trigger, **extra})
    except Exception:
        pass
    return result


def _prune() -> None:
    db.execute(
        "DELETE FROM job_runs WHERE id <= "
        "(SELECT id FROM job_runs ORDER BY id DESC LIMIT 1 OFFSET ?)", (_KEEP_RUNS,))


def _sweep_toast(job: str, result) -> str | None:
    """Return a brief human-readable line for the status bar, or None if nothing notable."""
    if not isinstance(result, dict):
        return None
    if result.get("skipped") or result.get("error"):
        return None
    parts = []
    n = result.get("new_items", 0) or 0
    if n > 0:
        parts.append(f"{n} new")
    triage = result.get("triage") or {}
    if isinstance(triage, dict):
        drafted = triage.get("drafted", 0) or 0
        if drafted > 0:
            parts.append(f"{drafted} drafted")
    classified = result.get("classified", 0) or 0
    if classified > 0 and not parts:
        parts.append(f"{classified} classified")
    needs = result.get("needs_attention", 0) or 0
    if needs > 0:
        parts.append(f"{needs} need attention")
    summarized = result.get("summarized", 0) or 0
    if summarized > 0:
        parts.append(f"{summarized} summarized")
    closed = result.get("closed", 0) or 0
    if closed > 0:
        parts.append(f"{closed} closed")
    # pr_status: tracked + closed
    tracked = result.get("tracked", 0) or 0
    if tracked > 0 and not parts:
        parts.append(f"{tracked} tracked")
    # chieff.handle_mentions (direct) or nested in teams sweep result
    chieff_r = result.get("chieff") or {}
    if isinstance(chieff_r, dict):
        ch_handled = chieff_r.get("handled", 0) or 0
        if ch_handled > 0:
            parts.append(f"{ch_handled} @chieff handled")
    handled = result.get("handled", 0) or 0
    if handled > 0:
        parts.append(f"{handled} handled")
    # github_digest: posted
    posted = result.get("posted", 0) or 0
    if posted > 0:
        parts.append(f"{posted} posted")
    # pr_readiness: checked + nudged
    checked = result.get("checked", 0) or 0
    nudged = result.get("nudged", 0) or 0
    if checked > 0 and not parts:
        parts.append(f"{checked} PRs checked")
    if nudged > 0:
        parts.append(f"{nudged} nudged")
    # knowledge sync: confluence + ado
    conf_changed = result.get("confluence_changed", 0) or 0
    ado_changed = result.get("ado_projects_changed", 0) or 0
    if conf_changed + ado_changed > 0:
        parts.append(f"{conf_changed + ado_changed} synced")
    # graph_reauth: launched auth-node.js, waiting on the user to finish sign-in
    if job == "graph_reauth" and result.get("started"):
        return f"Graph token expired - re-auth started on port {result['port']}, sign in to finish"
    return " · ".join(parts) if parts else None


def jobs_health() -> list[dict]:
    """Per-job health: last run, last success, consecutive-failure streak,
    24h run/failure counts and average duration."""
    recent = db.query("SELECT * FROM job_runs ORDER BY id DESC LIMIT 2000")
    day_ago = db.cutoff(hours=24)
    by_job: dict[str, list[dict]] = {}
    for r in recent:
        by_job.setdefault(r["job"], []).append(r)  # newest first

    out = []
    for job, runs in sorted(by_job.items()):
        last = runs[0]
        streak = 0
        for r in runs:
            if r["ok"] == 0:
                streak += 1
            elif r["ok"] == 1:
                break
        last_success = next((r for r in runs if r["ok"] == 1), None)
        day = [r for r in runs if r["started_at"] >= day_ago]
        durations = [r["duration_ms"] for r in day if r["duration_ms"] is not None]
        out.append({
            "job": job,
            "last_run": last["started_at"],
            "last_ok": last["ok"],
            "last_error": last["error"],
            "last_summary": last["summary"],
            "last_duration_ms": last["duration_ms"],
            "last_trigger": last["trigger"],
            "running": last["ok"] is None and last["finished_at"] is None,
            "last_success": last_success["started_at"] if last_success else None,
            "error_streak": streak,
            "runs_24h": len(day),
            "failures_24h": sum(1 for r in day if r["ok"] == 0),
            "avg_duration_ms": int(sum(durations) / len(durations)) if durations else None,
        })
    return out


def agent_health() -> dict:
    """Health of the LLM agent runs (cos_cost now also records duration/outcome)."""
    day_ago = db.cutoff(hours=24)
    rows = db.query(
        "SELECT label, model, cost_usd, duration_ms, ok, error, ts FROM cos_cost "
        "WHERE ts >= ? ORDER BY id DESC", (day_ago,))
    calls = len(rows)
    failures = sum(1 for r in rows if r["ok"] == 0)
    durations = [r["duration_ms"] for r in rows if r["duration_ms"]]
    by_label: dict[str, dict] = {}
    for r in rows:
        b = by_label.setdefault(r["label"] or "agent",
                                {"calls": 0, "failures": 0, "cost": 0.0})
        b["calls"] += 1
        b["failures"] += 1 if r["ok"] == 0 else 0
        b["cost"] += r["cost_usd"] or 0.0
    last_error = next((r for r in rows if r["ok"] == 0), None)
    return {
        "calls_24h": calls,
        "failures_24h": failures,
        "cost_24h": round(sum(r["cost_usd"] or 0.0 for r in rows), 4),
        "avg_duration_ms": int(sum(durations) / len(durations)) if durations else None,
        "last_call": rows[0]["ts"] if rows else None,
        "last_error": ({"ts": last_error["ts"], "label": last_error["label"],
                        "error": last_error["error"]} if last_error else None),
        "by_label": [{"label": k, **v, "cost": round(v["cost"], 4)}
                     for k, v in sorted(by_label.items())],
    }


def summary() -> dict:
    """Everything the Health tab needs in one call."""
    from . import mcp_clients, scheduler

    next_runs = {}
    try:
        for j in scheduler.scheduler.get_jobs():
            nrt = getattr(j, "next_run_time", None)
            next_runs[j.id] = nrt.isoformat() if nrt else None
    except Exception:
        pass

    jobs = jobs_health()
    for j in jobs:
        j["next_run"] = next_runs.get(j["job"])

    mcp = {}
    try:
        mcp = mcp_clients.get_manager().stats()
    except Exception as e:
        mcp = {"error": str(e)[:200]}

    db_stats = {}
    errors_24h = 0
    try:
        from .config import DB_PATH
        # All four COUNT queries in one statement using scalar subqueries.
        day_cutoff = db.cutoff(hours=24)
        counts = db.query(
            "SELECT "
            "(SELECT COUNT(*) FROM items) AS items, "
            "(SELECT COUNT(*) FROM audit_log) AS audit_rows, "
            "(SELECT COUNT(*) FROM drafts WHERE status='pending') AS pending_drafts, "
            "(SELECT COUNT(*) FROM audit_log "
            " WHERE action IN ('job_error','triage_error','knowledge_error') AND ts >= ?) AS errors_24h",
            (day_cutoff,))[0]
        db_stats = {
            "size_bytes": DB_PATH.stat().st_size if DB_PATH.exists() else 0,
            "items": counts["items"],
            "audit_rows": counts["audit_rows"],
            "pending_drafts": counts["pending_drafts"],
        }
        errors_24h = counts["errors_24h"] or 0
    except Exception:
        pass

    return {"at": db.now(), "jobs": jobs, "scheduled_but_never_ran":
            sorted(set(next_runs) - {j["job"] for j in jobs}),
            "mcp": mcp, "agent": agent_health(), "db": db_stats,
            "audit_errors_24h": errors_24h}
