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


def _pr_link(pr: dict) -> str:
    """HTML link showing 'repo#num'."""
    import html as _h
    repo = pr["repository"]["nameWithOwner"].split("/")[-1]
    url = pr["url"]
    return f'<a href="{url}">{_h.escape(repo)}#{pr["number"]}</a>'


def _pr_li(pr: dict, note: str = "") -> str:
    import html as _h
    link = _pr_link(pr)
    title = _h.escape(pr["title"][:80])
    author = _h.escape((pr.get("author") or {}).get("login", ""))
    return f"<li>{link} &mdash; {title}{(' &nbsp;' + note) if note else ''}" \
           f"{(' &nbsp;<em>by ' + author + '</em>') if author else ''}</li>"


def _html_digest(humans: list, bots: list, stuck: list, active_mine: list) -> str:
    import html as _h
    parts = ["<p><strong>GitHub pending-work digest</strong></p>"]
    if not humans and not bots and not stuck:
        parts.append("<p>Nothing pending on your side right now.</p>")
        return "".join(parts)
    if humans:
        parts.append(f"<p><strong>Waiting on your review</strong> ({len(humans)}):</p><ul>")
        for p in humans[:6]:
            parts.append(_pr_li(p))
        if len(humans) > 6:
            parts.append(f"<li><em>...and {len(humans) - 6} more</em></li>")
        parts.append("</ul>")
    if bots:
        by_repo: dict[str, int] = {}
        for p in bots:
            r = p["repository"]["nameWithOwner"].split("/")[-1]
            by_repo[r] = by_repo.get(r, 0) + 1
        grouped = ", ".join(f"{n} in <code>{_h.escape(r)}</code>" for r, n in by_repo.items())
        parts.append(f"<p><strong>Dependabot backlog:</strong> {grouped} (batch-review)</p>")
    if stuck:
        parts.append(f"<p><strong>Your PRs idle &ge;2d</strong> ({len(stuck)}):</p><ul>")
        for p in stuck[:5]:
            idle = _age_days(p["updatedAt"])
            badge = f'<strong style="color:#f59e0b">{idle}d idle</strong>'
            parts.append(_pr_li(p, badge))
        parts.append("</ul>")
    if active_mine:
        parts.append(f"<p><em>{len(active_mine)} of your PRs in motion (updated &lt;2d)</em></p>")
    return "".join(parts)


async def digest() -> dict:
    from .pr_review import paused
    if paused():
        return {"skipped": "pr_review paused (tuning)"}
    cfg = policy().get("pr_review", {})
    channels = [c for c in (cfg.get("digest_channels") or [])
                if c.get("team_id") and c.get("channel_id")]
    chat_id = None
    if not channels:
        topic = cfg.get("chat_topic")
        if not topic:
            return {"skipped": "no pr_review.digest_channels or chat_topic configured"}
        chat_id = _find_chat_id(await graph.chats_cached(), topic)
        if not chat_id:
            return {"error": f"chat '{topic}' not found"}

    # Fetch both PR lists concurrently.
    awaiting_me, mine = await asyncio.gather(
        _gh_search("--review-requested=@me"),
        _gh_search("--author=@me"),
    )

    bots = [p for p in awaiting_me if "dependabot" in (p.get("author") or {}).get("login", "")]
    humans = [p for p in awaiting_me if p not in bots]
    stuck = [p for p in mine if _age_days(p["updatedAt"]) >= 2]
    active_mine = [p for p in mine if p not in stuck]

    body = _html_digest(humans, bots, stuck, active_mine)
    if chat_id:
        result = await actions.assistant_chat_post(chat_id, body, kind="pr_digest", item_id=None)
    else:
        # Post to all channels concurrently; one channel's failure shouldn't lose
        # the audit trail (or block the digest) for the channels that succeeded.
        names = [c.get("name") or c["channel_id"] for c in channels]
        posts = await asyncio.gather(*[
            actions.assistant_channel_post(
                c["team_id"], c["channel_id"], body, kind="pr_digest", item_id=None)
            for c in channels
        ], return_exceptions=True)
        result = {n: (f"error: {p}" if isinstance(p, Exception) else p)
                  for n, p in zip(names, posts)}
    db.audit("pr_digest_posted", {"result": result, "chars": len(body)})
    from .. import vault
    vault.chieff_trace("GitHub", f"daily pending-work digest posted ({result})")
    return {"result": result, "preview": body[:400]}
