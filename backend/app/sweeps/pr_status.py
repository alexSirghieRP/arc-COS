"""PR approval tracker. Polls open PRs in enabled repos, derives review status,
round, and who we're waiting on, and keeps the board's PR Approvals tab current.

Runs automatically. PRs can be pulled out of the automation queue (settings
key pr_queue_disabled); pr_review honours the same list so a pulled PR is not
auto-reviewed either.
"""

import asyncio
import json
import logging
import time as _time

from .. import db, graph
from ..config import policy

# Cache gh repo list for 5 minutes (repos are added rarely, not per-sweep).
_repo_cache: list[str] = []
_repo_cache_ts: float = 0.0
_REPO_CACHE_TTL = 300

log = logging.getLogger("chief.sweep.pr_status")

ME_LOGINS = tuple(l.lower() for l in policy().get("me", {}).get("github_logins", []))


async def _gh_json(*args: str) -> list | dict | None:
    proc = await asyncio.create_subprocess_exec(
        "gh", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0:
        log.warning("gh failed: %s", err.decode()[:200])
        return None
    return json.loads(out.decode())


def queue_disabled() -> set:
    return set(json.loads(db.get_setting("pr_queue_disabled", "[]")))


def _chieff_rounds_batch(urls: list[str]) -> dict[str, int]:
    """Return {url: review_count} for all URLs in one query."""
    if not urls:
        return {}
    placeholders = ",".join("?" * len(urls))
    rows = db.query(
        f"SELECT json_extract(detail,'$.pr_url') AS pr_url, COUNT(*) AS n "
        f"FROM audit_log WHERE action='pr_reviewed' "
        f"AND json_extract(detail,'$.pr_url') IN ({placeholders}) "
        f"GROUP BY pr_url",
        tuple(urls))
    return {r["pr_url"]: r["n"] for r in rows}


def _derive(pr: dict) -> dict:
    """Status label + who we wait for, from GitHub review signals."""
    decision = pr.get("reviewDecision") or ""
    author = (pr.get("author") or {}).get("name") or (pr.get("author") or {}).get("login", "?")
    reviews = pr.get("latestReviews") or []
    requested = []
    for rr in pr.get("reviewRequests") or []:
        requested.append(rr.get("name") or rr.get("login") or rr.get("slug", "?"))
    alex_requested = any(
        (rr.get("login") or "").lower() in ME_LOGINS for rr in pr.get("reviewRequests") or [])

    if pr.get("isDraft"):
        return {"label": "Draft", "tone": "muted", "waiting_on": author}
    if alex_requested:
        return {"label": "Waiting on the user's review", "tone": "mine", "waiting_on": "you"}
    if decision == "APPROVED":
        return {"label": "Approved, ready to merge", "tone": "good", "waiting_on": author}
    if decision == "CHANGES_REQUESTED":
        return {"label": "Changes requested", "tone": "warn", "waiting_on": author}
    # REVIEW_REQUIRED or unset: waiting on reviewers
    who = ", ".join(requested) if requested else "reviewers"
    return {"label": "Awaiting review", "tone": "wait", "waiting_on": who}


async def sweep() -> dict:
    global _repo_cache, _repo_cache_ts
    cfg = policy().get("pr_review", {})
    owner = cfg.get("github_owner") or policy().get("github", {}).get("owner", "your-github-org")
    prefixes = cfg.get("batch_repo_prefixes", ["arc-"])
    disabled_repos = set(json.loads(db.get_setting("pr_review_repos_disabled", "[]")))

    if _time.time() - _repo_cache_ts > _REPO_CACHE_TTL or not _repo_cache:
        repos = await _gh_json("repo", "list", owner, "--limit", "300", "--json", "name")
        if repos is None:
            return {"error": "gh repo list failed"}
        _repo_cache = [r["name"] for r in repos
                       if any(r["name"].startswith(p) for p in prefixes)]
        _repo_cache_ts = _time.time()
    names = [n for n in _repo_cache if n not in disabled_repos]

    async def _fetch_repo(repo: str) -> list[tuple[str, dict]]:
        prs = await _gh_json(
            "pr", "list", "--repo", f"{owner}/{repo}", "--state", "open", "--limit", "50",
            "--json", "number,title,author,url,isDraft,reviewDecision,reviewRequests,"
                      "latestReviews,updatedAt")
        if not prs:
            return []
        results = []
        for pr in prs:
            url = pr["url"].lower()
            d = _derive(pr)
            reviewers = [{"who": (r.get("author") or {}).get("login", "?"),
                          "state": r.get("state", "")} for r in (pr.get("latestReviews") or [])]
            payload = {
                "number": pr["number"], "repo": repo, "url": pr["url"],
                "title": pr["title"],
                "author": (pr.get("author") or {}).get("name")
                          or (pr.get("author") or {}).get("login", "?"),
                "label": d["label"], "tone": d["tone"], "waiting_on": d["waiting_on"],
                "chieff_round": 0, "reviewers": reviewers,
                "review_decision": pr.get("reviewDecision"),
                "is_draft": pr.get("isDraft", False),
                "updated_at": pr.get("updatedAt"),
            }
            results.append((url, payload))
        return results

    # Fetch all repos concurrently (each is an independent gh API call).
    repo_results = await asyncio.gather(*[_fetch_repo(r) for r in names])

    # Batch-resolve chieff_round for all PRs (replaces N individual DB queries).
    all_urls = [url for pr_list in repo_results for url, _ in pr_list]
    round_counts = _chieff_rounds_batch([u.lower() for u in all_urls])
    for pr_list in repo_results:
        for url, payload in pr_list:
            payload["chieff_round"] = round_counts.get(url.lower(), 0)

    seen: set[str] = set()
    tracked = 0
    ts = db.now()
    with db.transaction():
        for pr_list in repo_results:
            for url, payload in pr_list:
                seen.add(url)
                content = json.dumps(payload)
                # 1 atomic upsert-or-update instead of upsert_item() + update_item() (2 ops → 1)
                db.execute(
                    "INSERT INTO items(source,type,external_id,sender,subject,content,"
                    "status,action_taken,received_at,created_at,updated_at) "
                    "VALUES('pr_approval','pr_approval',?,?,?,?,'classified',?,?,?,?) "
                    "ON CONFLICT(source,external_id) DO UPDATE SET "
                    "sender=excluded.sender,subject=excluded.subject,content=excluded.content,"
                    "status='classified',action_taken=excluded.action_taken,"
                    "received_at=excluded.received_at,updated_at=excluded.updated_at",
                    (url, payload["author"], payload["title"], content,
                     payload["label"], payload.get("updated_at"), ts, ts))
                tracked += 1

        # close out PRs no longer open — batch UPDATE instead of N individual calls
        close_rows = db.query("SELECT id, external_id FROM items "
                              "WHERE source='pr_approval' AND status != 'done'")
        closed_ids = [r["id"] for r in close_rows if r["external_id"] not in seen]
        closed = len(closed_ids)
        if closed_ids:
            close_ph = ",".join("?" * len(closed_ids))
            db.execute(
                f"UPDATE items SET status='done', action_taken='closed/merged', updated_at=? "
                f"WHERE id IN ({close_ph})",
                (db.now(),) + tuple(closed_ids))
    log.info("pr status: %d tracked, %d closed", tracked, closed)
    return {"tracked": tracked, "closed": closed}
