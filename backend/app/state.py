"""Runtime state: cached presence, away-mode, proposed focus."""

import asyncio
import json
import logging
import time
from datetime import datetime

from . import db, graph
from .config import policy

log = logging.getLogger("chief.state")

_presence_cache: dict = {}
_presence_ts: float = 0.0
_focus_cache: list = []
_focus_ts: float = 0.0


async def get_presence(ttl: int = 60) -> dict:
    global _presence_cache, _presence_ts
    if time.time() - _presence_ts > ttl:
        try:
            _presence_cache = await graph.presence()
            _presence_ts = time.time()
        except Exception as e:
            log.warning("presence fetch failed: %s", e)
    return _presence_cache


def is_away(presence: dict) -> bool:
    """Away-mode: manual override wins, else derived from Teams presence."""
    override = db.get_setting("away_override")
    if override in ("true", "false"):
        return override == "true"
    away_states = policy()["away"]["away_when"]
    return presence.get("availability") in away_states


def note_presence_transition(away: bool) -> None:
    """Persist when the current away stretch started; clear it when back."""
    since = db.get_setting("away_since")
    if away and not since:
        db.set_setting("away_since", db.now())
    elif not away and since:
        db.set_setting("away_since", "")


def away_minutes() -> float:
    """Minutes the user has been away in the current stretch (0 when not away)."""
    since = db.get_setting("away_since")
    if not since:
        return 0.0
    from datetime import timezone
    start = datetime.fromisoformat(since)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - start).total_seconds() / 60


def in_work_hours(now: datetime | None = None) -> bool:
    now = now or datetime.now().astimezone()
    wh = policy()["work_hours"]
    days = wh.get("days", ["mon", "tue", "wed", "thu", "fri"])
    if now.strftime("%a").lower()[:3] not in days:
        return False
    start_h, start_m = map(int, wh["start"].split(":"))
    end_h, end_m = map(int, wh["end"].split(":"))
    minutes = now.hour * 60 + now.minute
    return start_h * 60 + start_m <= minutes < end_h * 60 + end_m


async def _gh_prs(query_args: list[str]) -> list[dict]:
    proc = await asyncio.create_subprocess_exec(
        "gh", "search", "prs", *query_args, "--state=open", "--limit", "10",
        "--json", "title,url,repository,updatedAt",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        log.warning("gh search failed: %s", err.decode()[:200])
        return []
    return json.loads(out.decode())


async def proposed_focus(ttl: int = 300) -> list[dict]:
    """PRs awaiting me, my stuck PRs, aged pings, ADO sprint items."""
    global _focus_cache, _focus_ts
    if time.time() - _focus_ts < ttl and _focus_cache:
        return _focus_cache
    focus: list[dict] = []

    for pr in await _gh_prs(["--review-requested=@me"]):
        focus.append({"kind": "pr_review", "title": pr["title"],
                      "ref": pr["url"], "detail": pr["repository"]["nameWithOwner"]})
    for pr in await _gh_prs(["--author=@me"]):
        focus.append({"kind": "my_pr", "title": pr["title"],
                      "ref": pr["url"], "detail": pr["repository"]["nameWithOwner"]})

    aged = db.query(
        "SELECT * FROM items WHERE source='teams_chat' AND status IN ('new','classified') "
        "AND received_at < ? ORDER BY received_at ASC LIMIT 5", (db.cutoff(hours=4),))
    for it in aged:
        focus.append({"kind": "aged_ping", "title": f"{it['sender']}: {(it['content'] or '')[:80]}",
                      "ref": f"item:{it['id']}", "detail": it["subject"]})

    try:
        from .mcp_clients import get_manager
        ado = await get_manager().call("azure-devops", "wit_my_work_items", {
            "project": "OS Conversions Agent", "type": "assignedtome", "top": 5})
        work_items = ado.get("workItems", ado) if isinstance(ado, dict) else ado
        if isinstance(work_items, list):
            for wi in work_items[:5]:
                fields = wi.get("fields", {}) if isinstance(wi, dict) else {}
                title = fields.get("System.Title") or wi.get("title") or f"work item {wi.get('id')}"
                state = fields.get("System.State") or ""
                if state in ("Closed", "Done", "Removed"):
                    continue
                focus.append({"kind": "ado", "title": title, "ref": str(wi.get("id")),
                              "detail": state})
    except Exception as e:
        log.warning("ado focus failed: %s", e)

    _focus_cache, _focus_ts = focus, time.time()
    return focus
