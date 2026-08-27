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

# Simple in-process cache: GitHub full name → Teams display name (TTL not needed;
# names don't change often and the process restarts daily at most).
_name_cache: dict[str, str] = {}


async def _teams_display_name(owner_name: str, owner_login: str) -> str:
    """Try to resolve a GitHub full name to its Teams display name.
    Falls back gracefully to '@owner_name' on any error."""
    if not owner_name:
        return f"@{owner_login}"
    key = owner_name.lower()
    if key in _name_cache:
        return _name_cache[key]
    try:
        users = await graph.search_users(owner_name)
        if users:
            dn = users[0].get("displayName") or owner_name
            _name_cache[key] = f"@{dn}"
            return _name_cache[key]
    except Exception:
        pass
    _name_cache[key] = f"@{owner_name}"
    return _name_cache[key]


class _OutOfScope(Exception):
    """Raised when a PR's repo/queue toggle puts it out of review scope."""

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["approved", "needs_changes"],
            "description": "'approved' if safe to merge with no blocking issues; "
                           "'needs_changes' if there are critical/high findings that must be fixed first.",
        },
        "verified": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 4,
            "description": "2-4 specific things that were correct and checked out. "
                           "Name the exact logic, test case, or behavior. "
                           "e.g. 'the idempotent stamping logic', '171 tests pass', 'the empty-array edge case'.",
        },
        "test_result": {
            "type": "string",
            "description": "Concise statement about tests: 'tests pass', 'N tests added', "
                           "'no test coverage for this change', etc. Empty string if not applicable.",
        },
        "critical": {
            "type": "array",
            "description": "Must-fix issues only: correctness bugs, security, data loss, "
                           "breaking changes. Empty if the diff is clean.",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["critical", "high"]},
                    "title": {"type": "string"},
                    "file": {"type": "string"},
                    "detail": {"type": "string",
                               "description": "1-3 sentences, concrete, reference the code."},
                },
                "required": ["severity", "title", "file", "detail"],
            },
        },
        "followups": {
            "type": "array",
            "description": "Non-blocking suggestions: 'should_fix' (worth doing before Monday), "
                           "'nit' (minor polish). Omit if nothing worth noting.",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["should_fix", "nit"]},
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                },
                "required": ["severity", "title", "detail"],
            },
            "maxItems": 3,
        },
        "summary": {
            "type": "string",
            "description": "1-2 sentence high-level verdict for batch digest only. "
                           "Plain text, no file-by-file detail.",
        },
    },
    "required": ["verdict", "verified", "test_result", "critical", "followups", "summary"],
}

REVIEW_PROMPT = """Review this pull request as a senior engineer. The PR description and existing comments give you intent context; the diff is what you verify.

Verdict rules:
- "approved": safe to merge with no blocking issues (follow-ups are fine).
- "needs_changes": has critical/high findings that must be fixed before merge.

What to put in critical (must-fix): correctness bugs, security issues, data loss, breaking changes, production risks.
What to put in followups (should_fix / nit): subtle edge cases, test coverage gaps, minor code quality. Max 3.
What to omit entirely: style, naming, speculative concerns, anything you can't confirm from the diff.

For "verified": be concrete — name the exact logic, test cases, or behavior you checked, like "the empty-array branch in x()", "TC-006 classification", "171 tests pass". Not generic phrases like "logic is correct".
For "test_result": state the test situation precisely — "N tests added and pass", "existing tests pass", "no test coverage for this change".

IMPORTANT: you see ONLY the diff. Do NOT flag something as missing unless the diff itself removes it or the PR clearly depends on something absent. Prefer false negatives over false positives — a confident wrong finding wastes the team's time.

PR metadata (title, author, description, existing comments):
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


def paused() -> bool:
    """Global pause for ALL PR-review automation (reviews, chat posts, GitHub
    comments, digest). DB setting 'pr_review_paused' is the live board/API
    override; otherwise the policy.yaml default applies. Paused while the
    review quality is being tuned (see feedback 2026-07)."""
    v = db.get_setting("pr_review_paused")
    if v is not None:
        return v == "true"
    return bool(policy().get("pr_review", {}).get("paused", False))


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


_REVIEW_SEM = asyncio.Semaphore(3)  # max 3 concurrent reviews (GH + Teams rate limits)


async def _review_parallel(
    items: list[tuple[str, int]],
    chat_id: str,
    cfg: dict,
    *,
    chat_posts: bool = True,
) -> list[dict]:
    """Run up to 3 PR reviews concurrently and return successful results."""

    async def _one(url: str, item_id: int) -> dict | None:
        async with _REVIEW_SEM:
            try:
                return await _review_one(url, chat_id, item_id, cfg, chat_posts=chat_posts)
            except _OutOfScope:
                return None
            except Exception as e:
                log.exception("pr review failed for %s", url)
                db.update_item(item_id, status="dismissed",
                               action_taken=f"review failed: {str(e)[:150]}")
                db.audit("pr_review_error", {"pr_url": url, "error": str(e)[:300]},
                         item_id=item_id)
                return None

    raw = await asyncio.gather(*[_one(u, i) for u, i in items])
    return [r for r in raw if r is not None]


async def sweep() -> dict:
    if paused():
        return {"skipped": "pr_review paused (tuning)"}
    cfg = policy().get("pr_review", {})
    topic = cfg.get("chat_topic")
    if not topic:
        return {"skipped": "no pr_review.chat_topic in policy.yaml"}

    chats = await graph.chats_cached()
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
    results = await _review_parallel(new_items, chat_id, cfg, chat_posts=not batch)
    if batch and results:
        await _post_combined(chat_id, results)

    return {"new_prs": len(new_items), "reviewed": len(results)}


def _pr_link(url: str) -> str:
    """Clickable Teams/HTML link showing 'repo #num' instead of a raw URL."""
    m = re.match(r"https://github\.com/[^/]+/([^/]+)/pull/(\d+)", url, re.I)
    return f'<a href="{url}">{m.group(1)} #{m.group(2)}</a>' if m else f'<a href="{url}">{url}</a>'


async def _post_combined(chat_id: str, results: list[dict], label: str = ""):
    """One HTML chat message covering a whole batch of reviews."""
    emoji = policy().get("chieff", {}).get("emoji", "🤖")
    approved = [r for r in results if r.get("verdict") == "approved"]
    needs_changes = [r for r in results if r.get("verdict") != "approved"]
    # Fall back to critical count for old callers that don't have 'verdict'
    if not any("verdict" in r for r in results):
        approved = [r for r in results if not r["critical"]]
        needs_changes = [r for r in results if r["critical"]]
    title = f"PR review{(' &mdash; ' + label) if label else ''}"
    parts = [f"<p>{emoji} <b>{title}</b> &nbsp;|&nbsp; {len(results)} reviewed &middot; "
             f"<b>{len(needs_changes)} need changes</b> &middot; {len(approved)} approved</p>"]
    if needs_changes:
        parts.append("<p><b>🔴 Needs changes</b> &nbsp;<i>(findings on each PR)</i></p><ul>")
        for r in needs_changes:
            n = r["critical"]
            cnt = f"<b>{n} blocking</b>" if n > 1 else "1 blocking"
            parts.append(f"<li>{_pr_link(r['url'])} &nbsp;&middot;&nbsp; {r['owner_name']} "
                         f"&nbsp;&middot;&nbsp; {cnt}</li>")
        parts.append("</ul>")
    if approved:
        parts.append("<p><b>✅ Approved</b></p><ul>")
        for r in approved:
            blurb = f" &mdash; {r['summary'][:80]}" if r.get("summary") else ""
            parts.append(f"<li>{_pr_link(r['url'])} &nbsp;&middot;&nbsp; {r['owner_name']}{blurb}</li>")
        parts.append("</ul>")
    await actions.pr_review_chat_post(chat_id, "".join(parts), item_id=None)


def _format_review_chat(url: str, owner_tag: str, verdict: str,
                        verified: list[str], test_result: str,
                        critical: list[dict], followups: list[dict],
                        summary: str) -> str:
    """Build the Teams chat post in the team's short review style.

    Pattern: @Author #NNN approved/needs changes - [verified, test_result].
    [Follow-ups ranked: should-fix / nit].
    """
    import html as _h
    pr_num = re.search(r"/pull/(\d+)", url)
    num_label = f"#{pr_num.group(1)}" if pr_num else ""
    pr_anchor = f'<a href="{_h.escape(url)}">{num_label}</a>' if num_label else f'<a href="{url}">{url}</a>'

    # -- verdict clause --
    if verdict == "approved":
        verdict_word = "approved"
    else:
        verdict_word = f"<b>needs changes</b> ({len(critical)} blocking)"

    # -- what was verified --
    body_parts = []
    if verified:
        body_parts.append(", ".join(_h.escape(v) for v in verified[:3]))
        if body_parts[-1] and not body_parts[-1].endswith("."):
            if test_result:
                body_parts[-1] += f"; {_h.escape(test_result)}"
            else:
                body_parts[-1] += " all check out"
    elif test_result:
        body_parts.append(_h.escape(test_result))

    # -- critical summary --
    if critical:
        titles = "; ".join(_h.escape(c["title"])[:60] for c in critical[:2])
        extra = f" (+ {len(critical)-2} more)" if len(critical) > 2 else ""
        body_parts.append(f"blocking: {titles}{extra} — details on the PR")

    # -- follow-ups --
    followup_parts = []
    should_fixes = [f for f in followups if f.get("severity") == "should_fix"]
    nits = [f for f in followups if f.get("severity") == "nit"]
    if should_fixes:
        titles = "; ".join(_h.escape(f["title"])[:60] for f in should_fixes[:2])
        followup_parts.append(f"One should-fix: {titles}")
    if nits:
        cnt = len(nits)
        followup_parts.append(f"{cnt} nit{'s' if cnt > 1 else ''}: " +
                               "; ".join(_h.escape(f["title"])[:50] for f in nits[:2]))

    body = ("; ".join(body_parts) + ".") if body_parts else (
        _h.escape(summary[:200]) if summary else "clean diff, no findings.")

    followup_sentence = ". ".join(followup_parts) if followup_parts else ""

    text = f"{owner_tag} {pr_anchor} {verdict_word} &mdash; {body}"
    if followup_sentence:
        text += f" {followup_sentence}."

    return f"<p>{text}</p>"


async def _review_one(url: str, chat_id: str, item_id: int, cfg: dict,
                      chat_posts: bool = True) -> dict:
    # Hard scope guard: never review/post for a repo or PR taken out of scope,
    # even if it was queued before being disabled.
    if _repo_disabled(url) or _queue_disabled(url):
        db.update_item(item_id, status="dismissed", action_taken="out of review scope")
        raise _OutOfScope(url)
    rc, meta_raw = await _gh("pr", "view", url, "--json",
                             "title,author,state,baseRefName,headRefName,additions,deletions,"
                             "files,body,comments")
    if rc != 0:
        raise RuntimeError(f"gh pr view: {meta_raw[:200]}")
    meta = json.loads(meta_raw)
    # Never review a PR that already merged/closed: a review landing hours/days
    # after merge is pure noise (and shows up as a stale "safe to merge" call).
    state = (meta.get("state") or "").upper()
    if state and state != "OPEN":
        db.update_item(item_id, status="dismissed",
                       action_taken=f"skipped: PR already {state.lower()} (no late reviews)")
        raise _OutOfScope(url)
    author = meta.get("author") or {}
    owner_name = (author.get("name") or "").strip()
    owner_login = author.get("login", "unknown")
    owner_tag = await _teams_display_name(owner_name, owner_login)
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
    # PR description (body) gives crucial intent context for better reviews.
    pr_body = (meta.get("body") or "").strip()[:2000]
    # Existing review comments help understand what's already been discussed.
    existing_comments = [
        {"author": (c.get("author") or {}).get("login", "?"),
         "body": (c.get("body") or "")[:300]}
        for c in (meta.get("comments") or [])[-5:]  # last 5 only
    ]
    meta_brief = {
        "url": url, "title": title, "author": owner_login,
        "base": meta.get("baseRefName"), "head": meta.get("headRefName"),
        "additions": meta.get("additions"), "deletions": meta.get("deletions"),
        "files": [f.get("path") for f in (meta.get("files") or [])][:50],
        "description": pr_body or "(no description)",
        "existing_comments": existing_comments,
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
        raise RuntimeError("review produced no structured result (diff too large?)")
    critical = result.get("critical") or []
    followups = result.get("followups") or []
    verdict = result.get("verdict", "approved" if not critical else "needs_changes")
    verified = result.get("verified") or []
    test_result = (result.get("test_result") or "").strip()
    summary = (result.get("summary") or "").strip()
    db.audit("pr_reviewed", {"pr_url": url, "verdict": verdict, "summary": summary,
                             "critical_count": len(critical), "critical": critical,
                             "followups": followups},
             item_id=item_id)

    # 3. post findings on the GitHub PR (only when there's something to say)
    if critical or followups:
        lines = [f"## {emoji} Code review"]
        lines.append(f"**Verdict:** {'✅ approved' if verdict == 'approved' else '🔴 needs changes'}")
        if verified:
            lines.append(f"**Verified:** {', '.join(verified)}")
        if test_result:
            lines.append(f"**Tests:** {test_result}")
        lines.append("")
        if critical:
            lines.append(f"### Blocking ({len(critical)})")
            for c in critical:
                lines.append(f"\n**`{c['severity']}`** — {c['title']}  \n"
                              f"**File:** `{c['file']}`  \n{c['detail']}")
        if followups:
            lines.append(f"\n### Follow-ups ({len(followups)})")
            for f in followups:
                lines.append(f"\n**{f['severity'].replace('_', '-')}** — {f['title']}  \n{f['detail']}")
        await actions.post_github_comment(url, "\n".join(lines), item_id=item_id)

    # 4. Short chat message: "@Author #NNN approved - [verified]. [follow-ups]."
    if chat_posts:
        msg = _format_review_chat(url, owner_tag, verdict, verified, test_result,
                                  critical, followups, summary)
        await actions.pr_review_chat_post(chat_id, msg, item_id=item_id)

    db.update_item(item_id, status="done",
                   action_taken=f"reviewed ({verdict}): {len(critical)} critical, {len(followups)} followups")
    from .. import vault
    vault.chieff_trace("GitHub", f"reviewed PR {url} ({owner_login}): "
                                 f"{verdict}, {len(critical)} critical, {len(followups)} followups")
    return {"url": url, "owner_tag": owner_tag, "owner_name": owner_name or owner_login,
            "title": title, "critical": len(critical), "summary": summary,
            "verdict": verdict}


async def requeue_orphans() -> dict:
    """Review PRs whose review was interrupted between pickup and completion.

    Such items are stuck at status 'classified' ("picked for review"): the live
    sweep dedups by URL and only re-scans the recent chat window, so once their
    chat acks scroll away they would never be retried -- acked but never
    reviewed. This finds them by DB status (independent of the chat window) and
    reviews each directly. Already-merged/closed PRs are skipped (reviewing them
    days late is noise) and marked dismissed. Results post as ONE combined chat
    message -- no per-PR acks or @mentions for a recovery batch."""
    if paused():
        return {"skipped": "pr_review paused (tuning)"}
    cfg = policy().get("pr_review", {})
    topic = cfg.get("chat_topic")
    chat_id = _find_chat_id(await graph.chats_cached(), topic) if topic else None
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

    if paused():
        return {"skipped": "pr_review paused (tuning)"}
    cfg = policy().get("pr_review", {})
    topic = cfg.get("chat_topic")
    chat_id = _find_chat_id(await graph.chats_cached(), topic) if topic else None
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

    # First pass: apply cheap filters, collect candidates
    candidates: list[tuple[str, dict]] = []
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
        candidates.append((url, p))

    # Pre-batch: 1 query to find already-done PRs (replaces N individual status SELECTs)
    done_urls: set[str] = set()
    if candidates:
        cand_urls = [u for u, _ in candidates]
        done_ph = ",".join("?" * len(cand_urls))
        done_urls = {r["external_id"] for r in db.query(
            f"SELECT external_id FROM items WHERE source='pr_review' AND status='done' "
            f"AND external_id IN ({done_ph})",
            tuple(cand_urls))}

    for url, p in candidates:
        if url in done_urls:
            skipped_done += 1
            continue
        item_id, is_new = db.upsert_item(
            source="pr_review", type_="pr_review", external_id=url,
            conversation_id=chat_id, subject=topic, content=url)
        todo.append((url, item_id))

    results = await _review_parallel(todo, chat_id, cfg, chat_posts=False)
    failed = len(todo) - len(results) - sum(
        1 for u, _ in todo if _repo_disabled(u) or _queue_disabled(u))

    if results:
        await _post_combined(chat_id, results, label=f"last {days + 1} days")
    from .. import vault
    vault.chieff_trace("GitHub", f"batch-reviewed {len(results)} recent PRs "
                                 f"({failed} failed, {skipped_done} already done, "
                                 f"{skipped_bots} bot PRs skipped)")
    return {"reviewed": len(results), "failed": failed,
            "skipped_done": skipped_done, "skipped_bots": skipped_bots}
