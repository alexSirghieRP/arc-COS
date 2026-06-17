"""PR approval tracker. Polls open PRs in enabled repos, derives review status,
round, and who we're waiting on, and keeps the board's PR Approvals tab current.

Runs automatically. PRs can be pulled out of the automation queue (settings
key pr_queue_disabled); pr_review honours the same list so a pulled PR is not
auto-reviewed either.
"""

import asyncio
import json
import logging

from .. import db, graph
from ..config import policy

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


def _chieff_round(url: str) -> int:
    """How many times Chieff has reviewed this PR (our own review count)."""
    rows = db.query(
        "SELECT COUNT(*) AS n FROM audit_log WHERE action='pr_reviewed' "
        "AND json_extract(detail,'$.pr_url')=?", (url,))
    return rows[0]["n"] if rows else 0


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
    cfg = policy().get("pr_review", {})
    owner = cfg.get("github_owner") or policy().get("github", {}).get("owner", "your-github-org")
    prefixes = cfg.get("batch_repo_prefixes", ["arc-"])
    disabled_repos = set(json.loads(db.get_setting("pr_review_repos_disabled", "[]")))

    repos = await _gh_json("repo", "list", owner, "--limit", "300", "--json", "name")
    if repos is None:
        return {"error": "gh repo list failed"}
    names = [r["name"] for r in repos
             if any(r["name"].startswith(p) for p in prefixes) and r["name"] not in disabled_repos]

    seen: set[str] = set()
    tracked = 0
    for repo in names:
        prs = await _gh_json(
            "pr", "list", "--repo", f"{owner}/{repo}", "--state", "open", "--limit", "50",
            "--json", "number,title,author,url,isDraft,reviewDecision,reviewRequests,"
                      "latestReviews,updatedAt")
        if not prs:
            continue
        for pr in prs:
            url = pr["url"].lower()
            seen.add(url)
            d = _derive(pr)
            rnd = _chieff_round(url)
            reviewers = [{"who": (r.get("author") or {}).get("login", "?"),
                          "state": r.get("state", "")} for r in (pr.get("latestReviews") or [])]
            payload = {
                "number": pr["number"], "repo": repo, "url": pr["url"],
                "title": pr["title"],
                "author": (pr.get("author") or {}).get("name")
                          or (pr.get("author") or {}).get("login", "?"),
                "label": d["label"], "tone": d["tone"], "waiting_on": d["waiting_on"],
                "chieff_round": rnd, "reviewers": reviewers,
                "review_decision": pr.get("reviewDecision"),
                "is_draft": pr.get("isDraft", False),
                "updated_at": pr.get("updatedAt"),
            }
            item_id, _ = db.upsert_item(
                source="pr_approval", type_="pr_approval", external_id=url,
                subject=pr["title"], sender=payload["author"], content=json.dumps(payload),
                received_at=pr.get("updatedAt"))
            db.update_item(item_id, status="classified",
                           content=json.dumps(payload), subject=pr["title"],
                           action_taken=d["label"])
            tracked += 1

    # close out PRs no longer open
    closed = 0
    for row in db.query("SELECT id, external_id FROM items "
                        "WHERE source='pr_approval' AND status != 'done'"):
        if row["external_id"] not in seen:
            db.update_item(row["id"], status="done", action_taken="closed/merged")
            closed += 1
    log.info("pr status: %d tracked, %d closed", tracked, closed)
    return {"tracked": tracked, "closed": closed}
