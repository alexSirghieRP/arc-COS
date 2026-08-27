"""PR-readiness watchdog for the user's OWN open PRs.

Leans on the team's STANDARDS.md flow: open as draft, let CoS review and clear
should-fix items, keep CI green (the local pre-push hook runs the same gates),
and add a test for new behavior (esp. Control Tower routes). CoS can't run the
pre-push hook on the user's machine, but it can watch his PRs after they're pushed
and nudge him -- privately, in his Teams self-chat -- before he flips to Ready,
so human reviewers only ever see a clean PR.

Nothing is posted on the PR or to reviewers. Output is a board readiness list
plus a rate-limited self-chat note.
"""

import asyncio
import json
import logging
import re

from .. import actions, db
from ..config import policy

log = logging.getLogger("chief.sweep.pr_readiness")

STATE_KEY = "pr_readiness_state"


async def _gh_json(*args: str):
    proc = await asyncio.create_subprocess_exec(
        "gh", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0:
        log.warning("gh %s failed: %s", args[0] if args else "", err.decode()[:200])
        return None
    try:
        return json.loads(out.decode())
    except Exception:
        return None


def _ci_state(rollup) -> str:
    """Reduce statusCheckRollup to passing | failing | pending | none."""
    if not rollup:
        return "none"
    bad, pending = False, False
    for c in rollup:
        concl = (c.get("conclusion") or "").upper()
        state = (c.get("state") or "").upper()
        status = (c.get("status") or "").upper()
        if concl in ("FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE") or state == "FAILURE" or state == "ERROR":
            bad = True
        elif status in ("QUEUED", "IN_PROGRESS", "PENDING", "WAITING") or state == "PENDING" or (concl == "" and state == ""):
            pending = True
    if bad:
        return "failing"
    if pending:
        return "pending"
    return "passing"


def _review_statuses_batch(urls: list[str]) -> dict[str, dict]:
    """Batch version of _review_status: 2 queries total regardless of how many PRs."""
    if not urls:
        return {}
    lower_urls = [u.lower() for u in urls]
    ph = ",".join("?" * len(lower_urls))
    item_rows = db.query(
        f"SELECT id, external_id FROM items WHERE source='pr_review' AND external_id IN ({ph})",
        tuple(lower_urls))
    item_by_url = {r["external_id"]: r["id"] for r in item_rows}
    result: dict[str, dict] = {u: {"reviewed": False, "criticals": 0} for u in lower_urls}
    if not item_by_url:
        return result
    item_ids = list(item_by_url.values())
    id_ph = ",".join("?" * len(item_ids))
    aud_rows = db.query(
        f"SELECT item_id, detail FROM audit_log WHERE action='pr_reviewed' "
        f"AND item_id IN ({id_ph}) "
        f"AND id IN (SELECT MAX(id) FROM audit_log WHERE action='pr_reviewed' GROUP BY item_id)",
        tuple(item_ids))
    aud_by_item = {r["item_id"]: r["detail"] for r in aud_rows}
    for url, item_id in item_by_url.items():
        detail = aud_by_item.get(item_id)
        if not detail:
            result[url] = {"reviewed": False, "criticals": 0}
            continue
        try:
            d = json.loads(detail)
            result[url] = {"reviewed": True, "criticals": int(d.get("critical_count") or 0)}
        except Exception:
            result[url] = {"reviewed": True, "criticals": 0}
    return result


def _evaluate(meta: dict, cfg: dict, rev: dict | None = None) -> dict:
    files = [f.get("path", "") for f in (meta.get("files") or [])]
    is_draft = bool(meta.get("isDraft"))
    ci = _ci_state(meta.get("statusCheckRollup"))

    # tests for new behavior: any file under a test-required path, with no test
    # file in the same PR
    req_globs = cfg.get("test_required_globs", ["services/control_tower/app/api/"])
    markers = [m.lower() for m in cfg.get("test_file_markers", ["test", "spec", "__tests__"])]
    touches_required = [p for p in files if any(p.startswith(g) for g in req_globs)]
    has_test = any(any(m in p.lower() for m in markers) for p in files)
    tests_missing = bool(touches_required) and not has_test

    if rev is None:
        rev = _review_statuses_batch([meta["url"]]).get(meta["url"].lower(), {"reviewed": False, "criticals": 0})

    checks = [
        {"key": "draft", "label": "Opened as draft", "ok": is_draft,
         "detail": "draft" if is_draft else "marked Ready"},
        {"key": "ci", "label": "CI green", "ok": ci == "passing",
         "detail": ci, "pending": ci in ("pending", "none")},
        {"key": "tests", "label": "Test for new behavior", "ok": not tests_missing,
         "detail": (f"touches {len(touches_required)} Control Tower route file(s), no test in PR"
                    if tests_missing else ("test included" if touches_required else "no new-behavior paths"))},
        {"key": "review", "label": "CoS review clean", "ok": rev["reviewed"] and rev["criticals"] == 0,
         "detail": ("not yet reviewed" if not rev["reviewed"]
                    else (f"{rev['criticals']} open critical/high" if rev["criticals"] else "clean")),
         "advisory": not rev["reviewed"]},
    ]
    # blocking problems = the things that should stop a flip to Ready
    blockers = [c for c in checks if not c["ok"] and not c.get("pending") and not c.get("advisory")
                and c["key"] in ("ci", "tests")]
    if rev["reviewed"] and rev["criticals"] > 0:
        blockers.append(next(c for c in checks if c["key"] == "review"))
    ready = not blockers and ci != "failing"
    return {
        "url": meta["url"], "number": meta.get("number"), "title": meta.get("title", ""),
        "repo": (meta.get("headRepository") or {}).get("name") or meta.get("_repo", ""),
        "owner": ((meta.get("author") or {}).get("name") or "").strip()
                 or (meta.get("author") or {}).get("login", "?"),
        "is_draft": is_draft, "ci": ci, "checks": checks,
        "blockers": [c["key"] for c in blockers],
        "ready": ready,
        # the loud case: a Ready PR that isn't actually clean (a draft with
        # blockers is just normal WIP, not worth flagging)
        "needs_attention": bool(blockers) and not is_draft,
    }


async def sweep() -> dict:
    cfg = policy().get("pr_readiness", {})
    prefixes = cfg.get("repo_prefixes", ["arc-"])
    prs = await _gh_json("search", "prs", "--author=@me", "--state=open",
                         "--json", "number,title,url,repository", "--limit", "50")
    if prs is None:
        return {"error": "gh search failed"}

    scoped = [(p, (p.get("repository") or {}).get("name", "")) for p in prs
              if any((p.get("repository") or {}).get("name", "").startswith(pre) for pre in prefixes)]

    async def _fetch_one(p, repo):
        meta = await _gh_json("pr", "view", p["url"], "--json",
                              "isDraft,statusCheckRollup,files,reviewDecision,title,number,headRepository,author")
        if not meta:
            return None
        meta["url"] = p["url"]
        meta["_repo"] = repo
        return meta

    fetched_metas = await asyncio.gather(*[_fetch_one(p, r) for p, r in scoped])
    valid_metas = [m for m in fetched_metas if m is not None]

    # Batch-resolve review statuses: 2 queries total regardless of PR count.
    all_urls = [m["url"] for m in valid_metas]
    review_statuses = _review_statuses_batch(all_urls)

    results = [_evaluate(m, cfg, review_statuses.get(m["url"].lower())) for m in valid_metas]

    db.set_setting(STATE_KEY, json.dumps({"prs": results, "at": db.now()}))

    # nudge: batch the PRs needing attention that we haven't nudged about recently
    nudged = []
    if cfg.get("nudge", True):
        attention = [r for r in results if r["needs_attention"]]
        nudged_set = _batch_recently_nudged([r["url"] for r in attention], cfg)
        fresh = [r for r in attention if r["url"] not in nudged_set]
        if fresh:
            try:
                await actions.notify_alex(_nudge_body(fresh), kind="pr_readiness_nudge")
                for r in fresh:
                    db.audit("pr_readiness_nudge", {"pr_url": r["url"], "blockers": r["blockers"]})
                nudged = [r["url"] for r in fresh]
            except actions.SendBlocked as e:
                log.info("pr-readiness nudge blocked: %s", e)

    from .. import vault
    flagged = sum(1 for r in results if r["needs_attention"])
    if results:
        vault.chieff_trace("GitHub", f"PR readiness check: {len(results)} open PRs, "
                                     f"{flagged} need attention, {len(nudged)} nudged")
    return {"checked": len(results), "needs_attention": flagged, "nudged": len(nudged)}


def _batch_recently_nudged(urls: list[str], cfg: dict) -> set[str]:
    """Return the set of URLs nudged within renudge_hours — 1 query for all."""
    if not urls:
        return set()
    hours = int(cfg.get("renudge_hours", 20))
    ph = ",".join("?" * len(urls))
    rows = db.query(
        f"SELECT json_extract(detail,'$.pr_url') AS url FROM audit_log "
        f"WHERE action='pr_readiness_nudge' AND ts > ? "
        f"AND json_extract(detail,'$.pr_url') IN ({ph})",
        (db.cutoff(hours=hours),) + tuple(urls))
    return {r["url"] for r in rows}


def _nudge_body(prs: list[dict]) -> str:
    emoji = policy().get("chieff", {}).get("emoji", "🤖")
    lines = [f"<p>{emoji} <b>PR readiness</b> — {len(prs)} of your open PRs aren't clean yet "
             f"(before flipping to Ready):</p><ul>"]
    label = {"ci": "CI not green (run scripts/quality-check.sh)",
             "tests": "no test for new Control Tower behavior",
             "review": "open critical/high review findings"}
    for r in prs:
        items = ", ".join(label.get(b, b) for b in r["blockers"])
        state = "Ready" if not r["is_draft"] else "draft"
        m = re.match(r"https://github\.com/[^/]+/([^/]+)/pull/(\d+)", r["url"])
        name = f"{m.group(1)} #{m.group(2)}" if m else r["url"]
        lines.append(f'<li><a href="{r["url"]}">{name}</a> ({state}): {items}</li>')
    lines.append("</ul>")
    return "".join(lines)
