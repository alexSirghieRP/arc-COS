"""PR review automation for the configured Teams chat.

Pipeline per new PR link posted in the chat:
1. ack: reply in the chat tagging the owner that the PR was picked up
2. review: fetch metadata + diff via gh, agent reviews, keep only critical findings
3. post the critical findings as a GitHub comment on the PR
4. post a critical summary + link + @owner tag back in the same chat

Dedup is by PR URL, so the automation never reacts to its own posts (they
contain URLs already tracked).
"""

import asyncio
import json
import logging
import re

from .. import actions, db, graph
from ..agents import CHIEF, run_agent
from ..config import policy

log = logging.getLogger("chief.sweep.pr_review")

PR_URL_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")


class _OutOfScope(Exception):
    """Raised when a PR's repo/queue toggle puts it out of review scope."""

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string",
                    "description": "1-2 short sentences, high level verdict only: what the PR "
                                   "does and whether it's safe to merge. Plain text, no "
                                   "backticks, no file-by-file detail."},
        "critical": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["critical", "high"]},
                    "title": {"type": "string"},
                    "file": {"type": "string"},
                    "detail": {"type": "string"},
                },
                "required": ["severity", "title", "file", "detail"],
            },
        },
    },
    "required": ["summary", "critical"],
}

REVIEW_PROMPT = """Review this pull request. Report ONLY findings that genuinely matter:
correctness bugs, security issues, data loss, breaking changes, production risks.
Severity "critical" = must fix before merge; "high" = should fix. Do NOT report style,
naming, test coverage wishes, or speculative concerns. If the diff is clean, return an
empty critical list. detail: 1-3 sentences, concrete, reference the code. No em dashes.

PR metadata:
{meta}

Diff (may be truncated at {cap} chars):
{diff}
"""


async def _gh(*args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "gh", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    return proc.returncode, (out.decode() if proc.returncode == 0 else err.decode())


def _repo_disabled(url: str) -> bool:
    """Per-repo review toggle from the board's Repos tab."""
    m = re.match(r"https://github\.com/[^/]+/([^/]+)/pull/", url)
    repo = m.group(1) if m else ""
    disabled = set(json.loads(db.get_setting("pr_review_repos_disabled", "[]")))
    return repo in disabled


def _queue_disabled(url: str) -> bool:
    """Per-PR pull-out from the automation queue (PR Approvals tab)."""
    return url.lower() in set(json.loads(db.get_setting("pr_queue_disabled", "[]")))


def _find_chat_id(chats: list[dict], topic: str) -> str | None:
    for c in chats:
        if (c.get("topic") or "").strip().lower() == topic.strip().lower():
            return c["id"]
    return None


def _is_stale_orphan(item_id: int, timeout: int) -> bool:
    """True if a prior review was interrupted between pickup and completion.

    Such items are stuck at status 'classified' ("picked for review") and never
    advance to 'done'/'dismissed' -- a process restart or crash mid-review. The
    live sweep dedups by URL, so without this they would be orphaned forever:
    acked in chat but never actually reviewed. We require the item to be stale
    (untouched for longer than a review could legitimately take) so we never
    race a review that's still in flight in a concurrent sweep.
    """
    rows = db.query("SELECT status, updated_at FROM items WHERE id=?", (item_id,))
    if not rows:
        return False
    stale_after = max(10, (timeout // 60) * 2)  # minutes; 2x the review budget
    return (rows[0]["status"] == "classified"
            and (rows[0]["updated_at"] or "") < db.cutoff(minutes=stale_after))


async def sweep() -> dict:
    cfg = policy().get("pr_review", {})
    topic = cfg.get("chat_topic")
    if not topic:
        return {"skipped": "no pr_review.chat_topic in policy.yaml"}

    chats = await graph.list_chats(top=50)
    chat_id = _find_chat_id(chats, topic)
    if not chat_id:
        return {"error": f"chat '{topic}' not found"}

    # collect PR urls from recent messages (dedup via items makes this idempotent)
    urls: list[str] = []
    for m in await graph.read_chat_messages(chat_id, top=30):
        body = (m.get("body") or {}).get("content", "") or ""
        # github org/repo is case-insensitive; normalize so the same PR posted
        # with different casing is one item
        urls += [u.lower() for u in PR_URL_RE.findall(body)]

    review_timeout = int(cfg.get("review_timeout_seconds", 300))
    new_items: list[tuple[str, int]] = []
    for url in dict.fromkeys(urls):  # keep order, dedupe
        item_id, is_new = db.upsert_item(
            source="pr_review", type_="pr_review", external_id=url,
            conversation_id=chat_id, subject=topic, content=url)
        # Skip items we've already handled, but re-pick reviews that were
        # interrupted mid-flight (stuck at 'classified') so they aren't
        # orphaned -- acked in chat yet never reviewed.
        if not is_new and not _is_stale_orphan(item_id, review_timeout):
            continue
        if _repo_disabled(url):
            db.update_item(item_id, status="dismissed",
                           action_taken="repo disabled for review (Repos tab)")
            continue
        if _queue_disabled(url):
            db.update_item(item_id, status="dismissed",
                           action_taken="pulled from automation queue (PR Approvals tab)")
            continue
        new_items.append((url, item_id))

    # More than 3 reviews in one batch: one combined chat message, no per-PR posts.
    batch = len(new_items) > 3
    results = []
    for url, item_id in new_items:
        try:
            results.append(await _review_one(url, chat_id, item_id, cfg,
                                             chat_posts=not batch))
        except _OutOfScope:
            continue  # repo/PR out of scope, silently skip
        except Exception as e:
            log.exception("pr review failed for %s", url)
            db.update_item(item_id, status="dismissed",
                           action_taken=f"review failed: {str(e)[:150]}")
            db.audit("pr_review_error", {"pr_url": url, "error": str(e)[:300]},
                     item_id=item_id)
    if batch and results:
        await _post_combined(chat_id, results)

    return {"new_prs": len(new_items), "reviewed": len(results)}


def _pr_link(url: str) -> str:
    """Clickable Teams/HTML link showing 'repo #num' instead of a raw URL."""
    m = re.match(r"https://github\.com/[^/]+/([^/]+)/pull/(\d+)", url, re.I)
    return f'<a href="{url}">{m.group(1)} #{m.group(2)}</a>' if m else f'<a href="{url}">{url}</a>'


async def _post_combined(chat_id: str, results: list[dict], label: str = ""):
    """One HTML chat message covering a whole batch of reviews. Teams renders
    chat messages as HTML, so we use <p>/<ul>/<a>, not plain text + newlines.
    No @mentions here: pinging everyone in a digest is noise."""
    emoji = policy().get("chieff", {}).get("emoji", "🤖")
    clean = [r for r in results if not r["critical"]]
    flagged = [r for r in results if r["critical"]]
    title = f"PR review{(' &mdash; ' + label) if label else ''}"
    parts = [f"<p>{emoji} <b>{title}</b> &nbsp;|&nbsp; {len(results)} reviewed &middot; "
             f"<b>{len(flagged)} need attention</b> &middot; {len(clean)} clean</p>"]
    if flagged:
        parts.append("<p><b>⚠️ Needs attention</b> &nbsp;<i>(findings commented on each PR)</i></p><ul>")
        for r in flagged:
            n = r["critical"]
            cnt = f"<b>{n} findings</b>" if n > 1 else "1 finding"
            parts.append(f"<li>{_pr_link(r['url'])} &nbsp;&middot;&nbsp; {r['owner_name']} "
                         f"&nbsp;&middot;&nbsp; {cnt}</li>")
        parts.append("</ul>")
    if clean:
        parts.append("<p><b>✅ Clean &mdash; safe to merge</b></p><ul>")
        for r in clean:
            parts.append(f"<li>{_pr_link(r['url'])} &nbsp;&middot;&nbsp; {r['owner_name']}</li>")
        parts.append("</ul>")
    await actions.pr_review_chat_post(chat_id, "".join(parts), item_id=None)


async def _review_one(url: str, chat_id: str, item_id: int, cfg: dict,
                      chat_posts: bool = True) -> dict:
    # Hard scope guard: never review/post for a repo or PR taken out of scope,
    # even if it was queued before being disabled.
    if _repo_disabled(url) or _queue_disabled(url):
        db.update_item(item_id, status="dismissed", action_taken="out of review scope")
        raise _OutOfScope(url)
    rc, meta_raw = await _gh("pr", "view", url, "--json",
                             "title,author,state,baseRefName,headRefName,additions,deletions,files")
    if rc != 0:
        raise RuntimeError(f"gh pr view: {meta_raw[:200]}")
    meta = json.loads(meta_raw)
    author = meta.get("author") or {}
    owner_name = (author.get("name") or "").strip()
    owner_login = author.get("login", "unknown")
    owner_tag = f"@{owner_name}" if owner_name else owner_login
    title = meta.get("title", url)

    db.update_item(item_id, sender=owner_login, subject=title, status="classified",
                   action_taken="picked for review")
    db.audit("pr_review_picked", {"pr_url": url, "owner": owner_login, "title": title},
             item_id=item_id)

    emoji = policy().get("chieff", {}).get("emoji", "🤖")

    # No pickup ack: a PR link is only posted once the review is actually done
    # (step 4), so the chat never shows "reviewing..." for something unreviewed.

    # 2. review the diff
    cap = int(cfg.get("max_diff_chars", 60000))
    rc, diff = await _gh("pr", "diff", url)
    if rc != 0:
        raise RuntimeError(f"gh pr diff: {diff[:200]}")
    meta_brief = {
        "url": url, "title": title, "author": owner_login,
        "base": meta.get("baseRefName"), "head": meta.get("headRefName"),
        "additions": meta.get("additions"), "deletions": meta.get("deletions"),
        "files": [f.get("path") for f in (meta.get("files") or [])][:50],
    }
    result = await asyncio.wait_for(
        run_agent(
            CHIEF,
            REVIEW_PROMPT.format(meta=json.dumps(meta_brief, indent=1), cap=cap,
                                 diff=diff[:cap]),
            schema=REVIEW_SCHEMA, with_tools=False, max_turns=10,
            model=cfg.get("model", "claude-sonnet-4-6"), label="pr_review"),
        timeout=int(cfg.get("review_timeout_seconds", 300)))
    if not isinstance(result, dict):
        # Agent finished without structured output (e.g. huge diff hit max_turns).
        raise RuntimeError("review produced no structured result (diff too large?)")
    critical = result.get("critical") or []
    summary = (result.get("summary") or "").strip()
    db.audit("pr_reviewed", {"pr_url": url, "summary": summary,
                             "critical_count": len(critical), "critical": critical},
             item_id=item_id)

    # 3. post critical parts on the PR (skip the comment entirely when clean).
    # GitHub renders Markdown, so headers/lists/code spans format cleanly there.
    if critical:
        lines = [f"## {emoji} Automated review",
                 f"Critical findings only ({len(critical)}). Style nits omitted.", ""]
        for c in critical:
            lines.append(f"### `{c['severity']}` &mdash; {c['title']}")
            lines.append(f"**File:** `{c['file']}`")
            lines.append("")
            lines.append(c["detail"])
            lines.append("")
        await actions.post_github_comment(url, "\n".join(lines), item_id=item_id)

    # 4. short summary back in the chat, tagging the owner: verdict + link only
    if chat_posts:
        if critical:
            n = len(critical)
            cnt = f"{n} critical/high findings" if n > 1 else "1 critical/high finding"
            msg = (f"<p>{emoji} {owner_tag}, reviewed {_pr_link(url)}: <b>{cnt}</b>, "
                   f"details on the PR.</p>")
        else:
            msg = (f"<p>{emoji} {owner_tag}, reviewed {_pr_link(url)}: "
                   f"no critical findings.</p><p>{summary}</p>")
        await actions.pr_review_chat_post(chat_id, msg, item_id=item_id)
    db.update_item(item_id, status="done",
                   action_taken=f"reviewed: {len(critical)} critical finding(s)")
    from .. import vault
    vault.chieff_trace("GitHub", f"reviewed PR {url} ({owner_login}): "
                                 f"{len(critical)} critical/high finding(s)")
    return {"url": url, "owner_tag": owner_tag, "owner_name": owner_name or owner_login,
            "title": title, "critical": len(critical), "summary": summary}


async def requeue_orphans() -> dict:
    """Review PRs whose review was interrupted between pickup and completion.

    Such items are stuck at status 'classified' ("picked for review"): the live
    sweep dedups by URL and only re-scans the recent chat window, so once their
    chat acks scroll away they would never be retried -- acked but never
    reviewed. This finds them by DB status (independent of the chat window) and
    reviews each directly. Already-merged/closed PRs are skipped (reviewing them
    days late is noise) and marked dismissed. Results post as ONE combined chat
    message -- no per-PR acks or @mentions for a recovery batch."""
    cfg = policy().get("pr_review", {})
    topic = cfg.get("chat_topic")
    chat_id = _find_chat_id(await graph.list_chats(top=50), topic) if topic else None
    if not chat_id:
        return {"error": f"chat '{topic}' not found"}

    rows = db.query("SELECT id, external_id FROM items WHERE source='pr_review' "
                    "AND status='classified' ORDER BY id")
    results, failed, skipped_closed = [], 0, 0
    for r in rows:
        url, item_id = r["external_id"], r["id"]
        # Skip PRs that already merged/closed while the review was orphaned.
        rc, state_raw = await _gh("pr", "view", url, "--json", "state")
        if rc == 0 and (json.loads(state_raw).get("state") or "").upper() != "OPEN":
            skipped_closed += 1
            db.update_item(item_id, status="dismissed",
                           action_taken="orphaned review skipped: PR already merged/closed")
            continue
        try:
            results.append(await _review_one(url, chat_id, item_id, cfg, chat_posts=False))
        except _OutOfScope:
            continue
        except Exception as e:
            failed += 1
            log.exception("orphan review failed for %s", url)
            db.update_item(item_id, status="dismissed",
                           action_taken=f"review failed: {str(e)[:150]}")
            db.audit("pr_review_error", {"pr_url": url, "error": str(e)[:300]},
                     item_id=item_id)
    if results:
        await _post_combined(chat_id, results, label="recovered reviews")
    from .. import vault
    vault.chieff_trace("GitHub", f"recovered {len(results)} orphaned PR reviews "
                                 f"({failed} failed, {skipped_closed} already merged/closed)")
    return {"orphans": len(rows), "reviewed": len(results),
            "failed": failed, "skipped_closed": skipped_closed}


async def review_recent(days: int = 1) -> dict:
    """Review all team PRs created in the last `days`+today across the org,
    skipping bots and already-reviewed PRs. Per-PR GitHub comments for critical
    findings as usual; the chat gets ONE combined summary message."""
    import json as _json
    from datetime import date, timedelta

    cfg = policy().get("pr_review", {})
    topic = cfg.get("chat_topic")
    chat_id = _find_chat_id(await graph.list_chats(top=50), topic) if topic else None
    if not chat_id:
        return {"error": f"chat '{topic}' not found"}
    prefixes = cfg.get("batch_repo_prefixes", ["arc-"])
    owner = cfg.get("github_owner") or policy().get("github", {}).get("owner", "your-github-org")

    since = (date.today() - timedelta(days=days)).isoformat()
    rc, out = await _gh("search", "prs", f"--owner={owner}", f"created:>={since}",
                        "--json", "number,url,author,repository", "--limit", "200")
    if rc != 0:
        return {"error": f"gh search failed: {out[:200]}"}
    prs = _json.loads(out)

    todo: list[tuple[str, int]] = []
    skipped_bots = skipped_done = 0
    for p in prs:
        repo = p["repository"]["nameWithOwner"].split("/")[-1]
        login = (p.get("author") or {}).get("login", "")
        if not any(repo.startswith(pre) for pre in prefixes):
            continue
        if "bot" in login.lower():
            skipped_bots += 1
            continue
        url = p["url"].lower()
        if _repo_disabled(url) or _queue_disabled(url):
            continue
        item_id, is_new = db.upsert_item(
            source="pr_review", type_="pr_review", external_id=url,
            conversation_id=chat_id, subject=topic, content=url)
        if not is_new:
            rows = db.query("SELECT status FROM items WHERE id=?", (item_id,))
            if rows and rows[0]["status"] == "done":
                skipped_done += 1
                continue
        todo.append((url, item_id))

    results, failed = [], 0
    for url, item_id in todo:
        try:
            results.append(await _review_one(url, chat_id, item_id, cfg,
                                             chat_posts=False))
        except _OutOfScope:
            continue  # repo/PR out of scope, silently skip
        except Exception as e:
            failed += 1
            log.exception("batch review failed for %s", url)
            db.update_item(item_id, status="dismissed",
                           action_taken=f"review failed: {str(e)[:150]}")
            db.audit("pr_review_error", {"pr_url": url, "error": str(e)[:300]},
                     item_id=item_id)

    if results:
        await _post_combined(chat_id, results, label=f"last {days + 1} days")
    from .. import vault
    vault.chieff_trace("GitHub", f"batch-reviewed {len(results)} recent PRs "
                                 f"({failed} failed, {skipped_done} already done, "
                                 f"{skipped_bots} bot PRs skipped)")
    return {"reviewed": len(results), "failed": failed,
            "skipped_done": skipped_done, "skipped_bots": skipped_bots}
