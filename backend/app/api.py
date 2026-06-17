"""Board API: items, drafts, audit, settings, manual triggers."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import db

router = APIRouter(prefix="/api")


@router.get("/items")
def items(status: str | None = None, limit: int = 100):
    if status:
        return db.query(
            "SELECT * FROM items WHERE status=? ORDER BY received_at DESC LIMIT ?",
            (status, limit))
    return db.query("SELECT * FROM items ORDER BY received_at DESC LIMIT ?", (limit,))


@router.get("/connectors")
async def connectors():
    from .connectors import statuses
    return await statuses()


@router.get("/audit")
def audit(limit: int = 200):
    return db.query("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))


@router.get("/settings")
def settings():
    return {r["key"]: r["value"] for r in db.query("SELECT key, value FROM settings")}


class SettingUpdate(BaseModel):
    key: str
    value: str


@router.post("/settings")
def set_setting(body: SettingUpdate):
    allowed = {"dry_run", "kill_switch", "away_override"}
    if body.key not in allowed:
        raise HTTPException(400, f"settable keys: {sorted(allowed)}")
    db.set_setting(body.key, body.value)
    db.audit("setting_changed", {"key": body.key, "value": body.value}, actor="user")
    return {"ok": True}


class ApproveBody(BaseModel):
    body: str | None = None


class RejectBody(BaseModel):
    reason: str = ""


@router.post("/drafts/{draft_id}/approve")
async def approve_draft(draft_id: int, payload: ApproveBody):
    from . import actions
    rows = db.query("SELECT * FROM drafts WHERE id=? AND status='pending'", (draft_id,))
    if not rows:
        raise HTTPException(404, "no pending draft with that id")
    draft = rows[0]
    if payload.body and payload.body.strip():
        draft["body"] = payload.body
        db.execute("UPDATE drafts SET body=? WHERE id=?", (payload.body, draft_id))
    try:
        result = await actions.approved_send(draft)
    except actions.SendBlocked as e:
        raise HTTPException(409, str(e))
    db.execute("UPDATE drafts SET status='approved_sent', resolved_at=? WHERE id=?",
               (db.now(), draft_id))
    if draft["item_id"]:
        db.update_item(draft["item_id"], status="answered",
                       action_taken=f"approved and {result}")
    return {"ok": True, "result": result}


@router.post("/drafts/{draft_id}/reject")
def reject_draft(draft_id: int, payload: RejectBody):
    rows = db.query("SELECT * FROM drafts WHERE id=? AND status='pending'", (draft_id,))
    if not rows:
        raise HTTPException(404, "no pending draft with that id")
    db.execute("UPDATE drafts SET status='rejected', reject_reason=?, resolved_at=? WHERE id=?",
               (payload.reason, db.now(), draft_id))
    if rows[0]["item_id"]:
        db.update_item(rows[0]["item_id"], status="classified",
                       action_taken="draft rejected")
    db.audit("draft_rejected", {"draft_id": draft_id, "reason": payload.reason,
                                "body": rows[0]["body"]},
             item_id=rows[0]["item_id"], actor="user")
    return {"ok": True}


class LoopsRunPayload(BaseModel):
    ids: list[int]


@router.post("/loops/run")
async def loops_run(payload: LoopsRunPayload):
    if not payload.ids:
        raise HTTPException(400, "no loop ids selected")
    from .sweeps.open_loops import run_selected
    return await run_selected(payload.ids)


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
def cost_cos_detail():
    """Detailed CoS cost breakdown for the Cost tab (daily / by purpose / by model)."""
    from .cost import cos_detail
    return cos_detail()


_ado_cache: dict = {"ts": 0.0, "data": None}
_conf_cache: dict = {"ts": 0.0, "data": None}


@router.get("/ado-items")
def ado_items(refresh: bool = False):
    """the user's assigned Azure DevOps work items + states. Cached 5 min."""
    import time
    from .ado import my_items
    if refresh or _ado_cache["data"] is None or time.time() - _ado_cache["ts"] > 300:
        _ado_cache["data"] = my_items()
        _ado_cache["ts"] = time.time()
    return _ado_cache["data"]


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
async def repos():
    """Repos Chieff may review: org repos matching the configured prefixes,
    plus anything it has reviewed before. Toggle state lives in settings."""
    import asyncio as _asyncio
    import json as _json
    import re as _re
    from .config import policy
    cfg = policy().get("pr_review", {})
    owner = cfg.get("github_owner") or policy().get("github", {}).get("owner", "your-github-org")
    prefixes = cfg.get("batch_repo_prefixes", ["arc-"])
    names: set[str] = set()
    proc = await _asyncio.create_subprocess_exec(
        "gh", "repo", "list", owner, "--limit", "300", "--json", "name",
        stdout=_asyncio.subprocess.PIPE, stderr=_asyncio.subprocess.PIPE)
    out, _ = await proc.communicate()
    if proc.returncode == 0:
        names |= {r["name"] for r in _json.loads(out.decode())
                  if any(r["name"].startswith(p) for p in prefixes)}
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
    out = []
    for r in rows:
        try:
            pr = _json.loads(r["content"])
        except (TypeError, ValueError):
            continue
        url = pr["url"].lower()
        comments = db.query(
            "SELECT ts, json_extract(detail,'$.body') AS body FROM audit_log "
            "WHERE action='github_comment' AND json_extract(detail,'$.pr_url')=? "
            "ORDER BY id DESC LIMIT 5", (url,))
        pr["in_queue"] = url not in disabled
        pr["comments"] = [{"ts": c["ts"], "body": c["body"]} for c in comments if c["body"]]
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


async def _pr_readiness_sweep():
    from .sweeps.pr_readiness import sweep
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
    except Exception as e:
        raise HTTPException(
            502, f"Confluence publish failed (token may lack write access): {str(e)[:200]}")

    # 2. post the summary to the UseCases Tech Lead chat (guarded send path)
    topic = cfg.get("chat_topic")
    post_result = "skipped: no weekly_status.chat_topic configured"
    if topic:
        chat_id = None
        for c in await graph.list_chats(top=50):
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


@router.post("/sweep/{name}")
async def run_sweep(name: str):
    from .daily import backfill_week, evening_shutdown, morning_brief
    from .sweeps import chieff, email, open_loops, pr_review, teams, transcripts
    sweeps = {
        "teams": teams.sweep,
        "chieff": chieff.handle_mentions,
        "chieff_daily_update": chieff.daily_update,
        "knowledge": _knowledge_sync,
        "pr_review_recent": lambda: pr_review.review_recent(days=2),
        "pr_review_orphans": pr_review.requeue_orphans,
        "pr_readiness": _pr_readiness_sweep,
        "weekly_status": _weekly_status_generate,
        "pr_status": _pr_status,
        "email": email.sweep,
        "open_loops": open_loops.sweep,
        "meeting_summaries": transcripts.sweep,
        "pr_review": pr_review.sweep,
        "pr_digest": _pr_digest,
        "morning_brief": morning_brief,
        "evening_shutdown": evening_shutdown,
        "backfill_week": backfill_week,
    }
    if name not in sweeps:
        raise HTTPException(404, f"known sweeps: {sorted(sweeps)}")
    return await sweeps[name]()
