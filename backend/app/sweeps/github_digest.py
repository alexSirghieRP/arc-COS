"""GitHub pending-work digest posted to the configured Teams channels
(pr_review.digest_channels), falling back to the PR review chat.

What is waiting on the user (review requests) and what of the user's is waiting on
others, grouped so dependabot noise stays one line. Goes through the guarded
send path: kill switch, dry run and the write allowlist all apply.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone

from .. import actions, db, graph
from ..config import policy
from .pr_review import _find_chat_id

log = logging.getLogger("chief.sweep.gh_digest")


async def _gh_search(*args: str) -> list[dict]:
    proc = await asyncio.create_subprocess_exec(
        "gh", "search", "prs", *args, "--state=open", "--limit", "30",
        "--json", "number,title,url,repository,updatedAt,author",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0:
        log.warning("gh search failed: %s", err.decode()[:200])
        return []
    return json.loads(out.decode())


def _age_days(iso: str) -> int:
    updated = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - updated).days


def _line(pr: dict, suffix: str = "") -> str:
    repo = pr["repository"]["nameWithOwner"].split("/")[-1]
    return f"- `{repo}#{pr['number']}`: {pr['title'][:80]}{suffix} {pr['url']}"


async def digest() -> dict:
    cfg = policy().get("pr_review", {})
    channels = [c for c in (cfg.get("digest_channels") or [])
                if c.get("team_id") and c.get("channel_id")]
    chat_id = None
    if not channels:
        topic = cfg.get("chat_topic")
        if not topic:
            return {"skipped": "no pr_review.digest_channels or chat_topic configured"}
        chat_id = _find_chat_id(await graph.list_chats(top=50), topic)
        if not chat_id:
            return {"error": f"chat '{topic}' not found"}

    awaiting_me = await _gh_search("--review-requested=@me")
    mine = await _gh_search("--author=@me")

    bots = [p for p in awaiting_me if "dependabot" in (p.get("author") or {}).get("login", "")]
    humans = [p for p in awaiting_me if p not in bots]
    stuck = [p for p in mine if _age_days(p["updatedAt"]) >= 2]
    active_mine = [p for p in mine if p not in stuck]

    lines = ["GitHub status for the user (auto-digest):"]
    if humans:
        lines.append(f"Waiting on the user's review ({len(humans)}):")
        lines += [_line(p, f" (from {p['author']['login']})") for p in humans[:5]]
        if len(humans) > 5:
            lines.append(f"- and {len(humans) - 5} more")
    if bots:
        by_repo: dict[str, int] = {}
        for p in bots:
            by_repo[p["repository"]["nameWithOwner"].split("/")[-1]] = \
                by_repo.get(p["repository"]["nameWithOwner"].split("/")[-1], 0) + 1
        grouped = ", ".join(f"{n} in {r}" for r, n in by_repo.items())
        lines.append(f"Dependabot backlog: {grouped} (batch-review candidates)")
    if stuck:
        lines.append(f"the user's PRs waiting on others ({len(stuck)}):")
        lines += [_line(p, f" (idle {_age_days(p['updatedAt'])}d)") for p in stuck[:5]]
    if active_mine:
        lines.append(f"the user's PRs in motion: {len(active_mine)}")
    if len(lines) == 1:
        lines.append("Nothing pending on the user's side right now.")

    body = "\n".join(lines)
    if chat_id:
        result = await actions.assistant_chat_post(chat_id, body, kind="pr_digest", item_id=None)
    else:
        result = {}
        for c in channels:
            result[c.get("name") or c["channel_id"]] = await actions.assistant_channel_post(
                c["team_id"], c["channel_id"], body, kind="pr_digest", item_id=None)
    db.audit("pr_digest_posted", {"result": result, "lines": len(lines)})
    from .. import vault
    vault.chieff_trace("GitHub", f"daily pending-work digest posted ({result})")
    return {"result": result, "preview": body[:400]}
