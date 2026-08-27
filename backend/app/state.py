"""Runtime state: cached presence, away-mode, proposed focus."""

import asyncio
import html
import json
import logging
import time
from datetime import datetime

from . import db, graph
from .config import policy

log = logging.getLogger("chief.state")

_presence_cache: dict = {}
_presence_ts: float = 0.0
_presence_lock: "asyncio.Lock | None" = None
_focus_cache: list = []
_focus_ts: float = 0.0
_focus_lock: "asyncio.Lock | None" = None


def _get_presence_lock() -> "asyncio.Lock":
    global _presence_lock
    if _presence_lock is None:
        import asyncio as _aio
        _presence_lock = _aio.Lock()
    return _presence_lock


def _get_focus_lock() -> "asyncio.Lock":
    global _focus_lock
    if _focus_lock is None:
        import asyncio as _aio
        _focus_lock = _aio.Lock()
    return _focus_lock


async def get_presence(ttl: int = 120) -> dict:
    global _presence_cache, _presence_ts
    if time.time() - _presence_ts <= ttl:
        return _presence_cache  # fast path
    async with _get_presence_lock():
        if time.time() - _presence_ts <= ttl:
            return _presence_cache
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


def _ellip(s: str | None, n: int) -> str:
    """Word-boundary truncation with a real ellipsis (a hard slice cuts mid-word
    and looks broken on the board). Also decodes HTML entities Teams leaves in
    message bodies (&gt;, &amp;...) so titles read naturally."""
    s = html.unescape((s or "").strip())
    if len(s) <= n:
        return s
    return s[:n].rsplit(" ", 1)[0].rstrip(",;:") + "…"


async def proposed_focus(ttl: int = 300) -> list[dict]:
    """PRs awaiting me, my stuck PRs, aged pings, urgent B/C pings, ADO sprint items.
    Lock serializes concurrent cold-cache callers so gh subprocesses fire at most once."""
    global _focus_cache, _focus_ts
    if time.time() - _focus_ts < ttl and _focus_cache:
        return _focus_cache  # fast path: cache warm

    async with _get_focus_lock():
        # Double-check: another coroutine may have refreshed while we waited.
        if time.time() - _focus_ts < ttl and _focus_cache:
            return _focus_cache

        focus: list[dict] = []

        # Urgent pings first (tier C, or urgent flag, no reply yet)
        urgent_pings = db.query(
            "SELECT * FROM items WHERE source IN ('teams_chat','teams_channel') "
            "AND tier IN ('B','C') AND urgent=1 "
            "AND status IN ('new','classified','drafted') "
            "AND (snoozed_until IS NULL OR snoozed_until <= ?) LIMIT 5", (db.now(),))
        for it in urgent_pings:
            focus.insert(0, {
                "kind": "urgent_ping",
                "title": f"{it['sender']}: {_ellip(it['content'], 120)}",
                "ref": f"item:{it['id']}", "detail": it["subject"],
            })

        async def _ado_items():
            # The azure-devops MCP server's wit_my_work_items tool errors out
            # reliably in practice; ado.py's REST client (built for the My-items
            # tab) hits the same PAT-authenticated API directly and works.
            try:
                from .ado import my_items
                result = await asyncio.to_thread(my_items)
                return result.get("items") if isinstance(result, dict) else None
            except Exception as e:
                log.warning("ado focus failed: %s", e)
                return None

        prs_me, prs_author, ado_raw = await asyncio.gather(
            _gh_prs(["--review-requested=@me"]),
            _gh_prs(["--author=@me"]),
            _ado_items(),
        )
        for pr in prs_me:
            focus.append({"kind": "pr_review", "title": pr["title"],
                          "ref": pr["url"], "detail": pr["repository"]["nameWithOwner"]})
        for pr in prs_author:
            focus.append({"kind": "my_pr", "title": pr["title"],
                          "ref": pr["url"], "detail": pr["repository"]["nameWithOwner"]})

        aged = db.query(
            "SELECT * FROM items WHERE source='teams_chat' AND tier IN ('B','C') "
            "AND status IN ('new','classified') AND urgent=0 "
            "AND received_at < ? ORDER BY received_at ASC LIMIT 5", (db.cutoff(hours=4),))
        for it in aged:
            focus.append({"kind": "aged_ping", "title": f"{it['sender']}: {_ellip(it['content'], 120)}",
                          "ref": f"item:{it['id']}", "detail": it["subject"]})

        if ado_raw:
            for wi in ado_raw[:5]:
                if wi.get("bucket") in ("Done", "Removed"):
                    continue
                focus.append({"kind": "ado", "title": wi.get("title") or f"work item {wi.get('id')}",
                              "ref": str(wi.get("id")), "detail": wi.get("state")})

        _focus_cache, _focus_ts = focus, time.time()
    return _focus_cache
