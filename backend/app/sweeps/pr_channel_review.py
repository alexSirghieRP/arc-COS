"""PR channel review: auto first-pass review + ongoing thread conversation
for colleague PRs posted in the "PR Reviews" channel (owner, 2026-07-14).

Trigger (re-verified here, never just trusted from upstream):
- top-level message (no replyToId), never from the owner himself
- the PR's own GitHub repo is in pr_channel_review.repos (owner, 2026-07-20:
  a sender allowlist was tried first, scoped to the team's workstream, after CoS
  mishandled an out-of-scope PR from someone
  outside that workstream - but an allowed sender also posts PRs
  outside the main repo, and CoS kept reviewing those. This gate
  is per-PR-URL and independent of who posted it - repo membership is a more
  precise "is this our work" signal than sender identity, and (owner,
  2026-08-19) the sender allowlist was dropped entirely: people stopped
  tagging CoS, and untagged posts from anyone outside the hardcoded name
  list were being silently skipped even when the PR was
  the main repo itself.)
  - EXCEPTION: an explicit @mention of the owner bypasses the repo allowlist
    above (owner, 2026-07-22: "if someone tags me... CoS should do pr review
    even if is not the main repo - BUT JUST IF THEY TAG ME").
    Being personally, deliberately tagged outranks the repo scoping; a plain
    unaddressed post from an out-of-scope repo still gets filtered as before.
- body contains >=1 GitHub PR URL
- an LLM judges it a genuine review request - ANY PR request counts (owner,
  2026-07-14: not just ones that tag him or use one exact phrase) - "up for
  your review", "back to you", a plain tag, whatever wording - as opposed to
  a status update or approval announcement that just happens to link a PR.

Pipeline per matching message:
1. review each PR (gh fetch + diff, same reviewer prompt/schema as pr_review.py)
2. post a GitHub PR comment for any critical/followup findings
3. reply in the SAME Teams thread with a conversational verdict
4. track the thread (pr_thread table) so it keeps the conversation going: a
   later reply that @mentions the owner OR reads like "back to you"/"can you
   check" gets a response (never on a plain unaddressed reply - CoS doesn't
   jump into a conversation the owner is already having himself). This works even
   in threads CoS never started (e.g. the owner's own manual review posts) -
   being addressed there bootstraps tracking on the spot. A reply that comes
   with new commits (head SHA changed) or asks for another look triggers a
   fresh review pass before CoS answers, so it never claims something is
   fixed without re-checking the diff.

Every PR is attempted at most once per outcome (items table, source=
pr_channel_review, keyed by URL): a permanently-dismissed PR (already
closed/merged, fetch failed) is never retried just because the trigger
matched again - that was the cause of the 2026-07-14 repeated-reaction bug.
"""

import asyncio
import html as _html
import json
import logging
import re

from .. import actions, db, graph
from ..agents import CHIEF, AgentDef, run_agent
from ..config import policy
from .pr_review import PR_URL_RE, REVIEW_PROMPT, REVIEW_SCHEMA

log = logging.getLogger("chief.sweep.pr_channel_review")

# Reaction sequence on every message CoS acts on: 👀 the moment it's picked
# up, then ✅ (clean/handled) or ❌ (blocking findings) once done - a visible
# status teammates can read at a glance (owner, 2026-07-14).
_REACT_SEEN = "👀"
_REACT_OK = "✅"
_REACT_BLOCKED = "❌"

_RECHECK_RE = re.compile(
    r"back to you|pushed|updated|please\s*(?:re-?)?check|another look|"
    r"take a look again|fixed|addressed", re.I)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_CODE_RE = re.compile(r"`([^`]+)`")

# Safety net for the 2026-07-20 incident: CONVO_AGENT drafted a literal
# clarifying question ("I need the PR author's exact full display name...")
# and it got posted straight to the channel instead of a real verdict - the
# calling code trusted whatever text came back. This catches that class of
# reply so it's discarded (falls back to the plain/no-reply path) rather than
# posted verbatim.
_META_QUESTION_RE = re.compile(
    r"who submitted|drop the name in|exact full display name|before i post|"
    r"quick flag|need (?:the|your|to know)", re.I)


def _looks_like_meta_question(text: str) -> bool:
    return bool(_META_QUESTION_RE.search(text))


def _md_to_html(text: str) -> str:
    """Minimal, controlled markdown -> Teams HTML: '**bold**', `code`, and
    '- bullet' lines. graph.to_html() and the Graph server's own auto-detection
    both guess at plain-text-vs-markdown-vs-html, and that guess sometimes
    collapses bullets/line breaks into a wall of text (owner, 2026-07-14) -
    building real <p>/<ul><li>/<b>/<code> tags here sidesteps both guesses."""
    out: list[str] = []
    bullets: list[str] = []

    def _flush():
        if bullets:
            out.append("<ul>" + "".join(f"<li>{b}</li>" for b in bullets) + "</ul>")
            bullets.clear()

    for line in text.strip().split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        esc = _html.escape(stripped)
        esc = _BOLD_RE.sub(r"<b>\1</b>", esc)
        esc = _CODE_RE.sub(r"<code>\1</code>", esc)
        if stripped.startswith("- "):
            bullets.append(esc[2:].strip())
            continue
        _flush()
        out.append(f"<p>{esc}</p>")
    _flush()
    return "".join(out)


CLASSIFY_AGENT = AgentDef(
    name="pr_channel_classify",
    system_prompt=(
        "You classify one Teams message from a PR review channel. Decide "
        "whether it is a REQUEST for someone to review, check, approve, or "
        "look again at the pull request(s) it references - any phrasing "
        "counts (\"up for review\", \"up for your review\", \"ready for "
        "review\", \"back to you\", \"can you check\", \"please take a "
        "look\", appreciate an approval/eyes, or just a plain @mention with "
        "no other framing) - as opposed to a status update, an approval/merge "
        "announcement, or unrelated chatter that just happens to link a PR. "
        "It does not matter who the message is addressed to - any genuine "
        "review request counts. When genuinely ambiguous, lean toward true: "
        "a missed review request is worse than an extra look at a fine PR."
    ),
)
CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "is_review_request": {"type": "boolean"},
    },
    "required": ["is_review_request"],
}


async def _is_review_request(body: str) -> bool:
    # A timeout/error here must never crash the whole sweep (owner, 2026-07-16:
    # this exact call, unguarded, took down an entire run and everything
    # after it in the same tick - including PRs that had nothing to do with
    # this message). Nothing is persisted on a failed classification, so
    # returning False just means "try again next tick," never a false
    # permanent dismissal.
    try:
        verdict = await run_agent(
            CLASSIFY_AGENT, f"# Message\n{body}",
            schema=CLASSIFY_SCHEMA, with_tools=False, max_turns=2,
            model="claude-haiku-4-5-20251001", label="pr_channel_classify", timeout=45)
        return bool(verdict.get("is_review_request"))
    except Exception:
        log.warning("pr_channel_review: classify failed, skipping this tick", exc_info=True)
        return False

CONVO_AGENT = AgentDef(
    name="pr_channel_convo",
    system_prompt=(
        "You are CoS, standing in for the owner as first-pass reviewer in the "
        "team's PR Reviews channel. Talk like a sharp, direct teammate doing code "
        "review, not a bot. No corporate tone, no 'as an AI', no sign-offs, no "
        "em dashes.\n\n"
        "Open by actually tagging the colleague, not just naming them: write "
        "'@' immediately followed by their EXACT full display name as given in "
        "the context (e.g. '@Jane Doe') - that literal text is what turns "
        "into a real Teams mention, so get the name exactly right, character "
        "for character, no nicknames or first-name-only.\n\n"
        "STRICT LENGTH RULE - this is a chat message, not a code review doc; "
        "the full GitHub write-up already has every detail, so this is only "
        "the scannable summary:\n"
        "- One line up front: overall verdict (approved / needs changes) across "
        "everything you're covering.\n"
        "- Per PR, AT MOST 3-4 lines total: a bullet with the PR number + "
        "one-line verdict, then at most 1-2 short bullet sub-points naming "
        "ONLY the must-know should-fix items (max ~12 words each, name the "
        "thing, not a paragraph explaining it). Skip nits entirely here - "
        "they're already on the PR.\n"
        "- No prose paragraphs, no restating the code, no 'the good/the bad' "
        "sections, no walking through what was verified in detail - that's "
        "what the GitHub comment already covers. Use '-' bullets, not numbered "
        "lists or headers.\n"
        "- A conversational follow-up reply (not a first pass): 1-3 sentences, "
        "direct answer first, still no bullets needed for a single point.\n\n"
        "Ground every claim in the data you're given - never say something is "
        "fixed, tested, correct, or resolved unless the findings actually show "
        "it. If asked a question, answer directly from what you verified; "
        "never invent behavior you didn't check."
    ),
)
CONVO_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string",
                  "description": "what to say back in the thread"},
    },
    "required": ["reply"],
}


def _cfg() -> dict:
    return policy().get("pr_channel_review", {}) or {}


_PR_REPO_RE = re.compile(r"github\.com/[\w.-]+/([\w.-]+)/pull/\d+", re.I)


def _pr_repo(url: str) -> str | None:
    m = _PR_REPO_RE.search(url)
    return m.group(1) if m else None


def _repo_allowed(url: str, cfg: dict) -> bool:
    """Repo allowlist (owner, 2026-07-20; sole scoping signal for untagged
    messages as of 2026-08-19 - see module docstring). This is a per-PR-URL
    gate, independent of who posted it. Empty configured
    pr_channel_review.repos list means no restriction from that side.

    Also consults the Repos board's pr_review_repos_disabled setting (same
    on/off toggle used by the older pr_review.py chat automation) so the one
    UI in the app actually governs both automations, rather than needing a
    separate toggle/policy edit per bot (owner, 2026-07-20, same day)."""
    repo = (_pr_repo(url) or "").lower()
    if not repo:
        return False
    allowed = cfg.get("repos") or []
    if allowed and repo not in {r.strip().lower() for r in allowed}:
        return False
    disabled = {r.lower() for r in json.loads(db.get_setting("pr_review_repos_disabled", "[]"))}
    return repo not in disabled


def paused() -> bool:
    """DB setting 'pr_channel_review_paused' is the live override; otherwise
    policy.yaml's default applies. Kept separate from the older pr_review
    pause (this automation is scoped much more narrowly)."""
    v = db.get_setting("pr_channel_review_paused")
    if v is not None:
        return v == "true"
    return bool(_cfg().get("paused", False))


async def _gh(*args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "gh", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    return proc.returncode, (out.decode() if proc.returncode == 0 else err.decode())


async def _review_pr(url: str, cfg: dict) -> dict | None:
    """gh fetch + diff review. Returns {'out_of_scope': reason} for a closed/
    merged PR, None on fetch failure, or the review's structured result
    (plus 'title' and 'head_sha') on success."""
    rc, meta_raw = await _gh("pr", "view", url, "--json",
                             "title,author,state,baseRefName,headRefName,headRefOid,"
                             "additions,deletions,files,body,comments")
    if rc != 0:
        log.warning("gh pr view failed for %s: %s", url, meta_raw[:200])
        return None
    meta = json.loads(meta_raw)
    state = (meta.get("state") or "").upper()
    if state and state != "OPEN":
        return {"out_of_scope": f"PR already {state.lower()}"}
    cap = int(cfg.get("max_diff_chars", 60000))
    rc, diff = await _gh("pr", "diff", url)
    if rc != 0:
        log.warning("gh pr diff failed for %s: %s", url, diff[:200])
        return None
    pr_body = (meta.get("body") or "").strip()[:2000]
    existing_comments = [
        {"author": (c.get("author") or {}).get("login", "?"),
         "body": (c.get("body") or "")[:300]}
        for c in (meta.get("comments") or [])[-5:]
    ]
    meta_brief = {
        "url": url, "title": meta.get("title", url),
        "author": (meta.get("author") or {}).get("login"),
        "base": meta.get("baseRefName"), "head": meta.get("headRefName"),
        "additions": meta.get("additions"), "deletions": meta.get("deletions"),
        "files": [f.get("path") for f in (meta.get("files") or [])][:50],
        "description": pr_body or "(no description)",
        "existing_comments": existing_comments,
    }
    result = await asyncio.wait_for(
        run_agent(
            CHIEF, REVIEW_PROMPT.format(meta=json.dumps(meta_brief, indent=1), cap=cap,
                                        diff=diff[:cap]),
            schema=REVIEW_SCHEMA, with_tools=False, max_turns=10,
            model=cfg.get("model", "claude-sonnet-4-6"), label="pr_channel_review"),
        timeout=int(cfg.get("review_timeout_seconds", 300)))
    if not isinstance(result, dict):
        return None
    result["title"] = meta.get("title", url)
    result["head_sha"] = meta.get("headRefOid")
    return result


def _findings_comment(result: dict) -> str | None:
    """Structured GitHub comment (markdown), same shape as pr_review.py's."""
    critical = result.get("critical") or []
    followups = result.get("followups") or []
    if not (critical or followups):
        return None
    emoji = policy().get("chieff", {}).get("emoji", "🤖")
    verdict = result.get("verdict", "approved" if not critical else "needs_changes")
    lines = [f"## {emoji} Code review",
             f"**Verdict:** {'✅ approved' if verdict == 'approved' else '🔴 needs changes'}"]
    verified = result.get("verified") or []
    if verified:
        lines.append(f"**Verified:** {', '.join(verified)}")
    test_result = (result.get("test_result") or "").strip()
    if test_result:
        lines.append(f"**Tests:** {test_result}")
    lines.append("")
    if critical:
        lines.append(f"### Blocking ({len(critical)})")
        for c in critical:
            lines.append(f"\n**`{c['severity']}`** - {c['title']}  \n"
                         f"**File:** `{c['file']}`  \n{c['detail']}")
    if followups:
        lines.append(f"\n### Follow-ups ({len(followups)})")
        for f in followups:
            lines.append(f"\n**{f['severity'].replace('_', '-')}** - {f['title']}  \n{f['detail']}")
    return "\n".join(lines)


def _review_submit_body(result: dict, has_findings_comment: bool) -> str:
    """Short body for the FORMAL GitHub review submission (distinct from
    _findings_comment, which - when present - is posted separately as its own
    PR comment). Kept short so detail never gets duplicated across both."""
    emoji = policy().get("chieff", {}).get("emoji", "🤖")
    critical = result.get("critical") or []
    verdict = result.get("verdict", "approved" if not critical else "needs_changes")
    if verdict == "approved":
        bits = [f"{emoji} Approved."]
        if result.get("summary"):
            bits.append(result["summary"])
        if has_findings_comment:
            bits.append("Non-blocking follow-ups noted in a separate comment.")
        return " ".join(bits)
    return (f"{emoji} Requesting changes - {len(critical)} blocking finding(s), "
            "see the review comment above for detail.")


_SUBMIT_ATTEMPTS = 3
_SUBMIT_BACKOFF_S = 4


def _unsubmitted_note(urls: list[str]) -> str:
    """One honest line for the Teams reply when the verdict reached GitHub's
    API but the review submission didn't."""
    nums = []
    for u in urls:
        m = re.search(r"/pull/(\d+)", u)
        nums.append(f"#{m.group(1)}" if m else u)
    which = ", ".join(nums)
    return (f"Couldn't post the formal GitHub review on {which} (GitHub is "
            f"returning errors) - the verdict above stands, but it needs a "
            f"manual approve on the PR.")


async def _submit_gh_review(url: str, result: dict, has_findings_comment: bool,
                            item_id: int | None) -> bool:
    """Actually submit the review verdict on GitHub (Approve / Request
    changes) - not just say it in Teams/a PR comment (owner, 2026-07-21: a
    teammate's PR #830 exchange showed CoS saying "approved" in prose while
    never touching the PR's real review state). Never raises - a failed
    submission must not crash the review pipeline; the Teams reply and PR
    comment still went out either way.

    Returns whether the review actually landed. GitHub 503s in bursts (owner,
    2026-08-17: exactly this dropped #962's approval on the floor while CoS
    announced it as approved in the channel), so a failure is retried before
    being reported - and it IS reported, because callers have to tell the
    channel the truth rather than silently claim a review that isn't there."""
    critical = result.get("critical") or []
    verdict = result.get("verdict", "approved" if not critical else "needs_changes")
    event = "approve" if verdict == "approved" else "request_changes"
    body = _review_submit_body(result, has_findings_comment)
    for attempt in range(1, _SUBMIT_ATTEMPTS + 1):
        try:
            await actions.submit_github_review(url, body, event, item_id=item_id)
            return True
        except Exception as e:
            log.warning("pr_channel_review: gh pr review submit failed for %s "
                        "(attempt %d/%d): %s", url, attempt, _SUBMIT_ATTEMPTS, e)
            if attempt < _SUBMIT_ATTEMPTS:
                await asyncio.sleep(_SUBMIT_BACKOFF_S * attempt)
    return False


async def _react(team_id: str, channel_id: str, message_id: str, emoji: str,
                 item_id: int | None = None) -> None:
    try:
        await actions.assistant_channel_react(team_id, channel_id, message_id, emoji,
                                              kind="pr_channel_react", item_id=item_id)
    except Exception as e:
        log.warning("pr_channel_review: reaction %s on %s failed: %s", emoji, message_id, e)


async def _compose_reply(context_prompt: str, model: str) -> str:
    verdict = await run_agent(
        CONVO_AGENT, context_prompt, schema=CONVO_SCHEMA, with_tools=False,
        max_turns=4, model=model, label="pr_channel_convo", timeout=90)
    return (verdict.get("reply") or "").strip()


def _review_brief(url: str, result: dict) -> dict:
    return {"pr_url": url, "title": result.get("title"), "verdict": result.get("verdict"),
            "verified": result.get("verified"), "test_result": result.get("test_result"),
            "critical": result.get("critical"), "followups": result.get("followups"),
            "summary": result.get("summary")}


async def _phase_new_reviews(team_id: str, channel_id: str, messages: list[dict],
                             cfg: dict, me_id: str) -> list[dict]:
    results = []
    for m in messages:
        message_id = None
        try:
            if m.get("replyToId"):
                continue  # only top-level "up for review" posts start a new thread
            message_id = m.get("id")
            if not message_id:
                continue
            author = (m.get("from") or {}).get("user") or {}
            author_name = author.get("displayName") or ""
            if author.get("id") == me_id:
                continue
            # An explicit @mention of the owner overrides the repo scoping gate
            # below - being personally, deliberately tagged is a stronger
            # signal than "posted in this channel" and matters more than
            # which repo it came from (owner, 2026-07-22: "if someone tags
            # me... CoS should do pr review even if is not
            # the main repo - BUT JUST IF THEY TAG ME").
            tagged = _mentions_me(m.get("mentions") or [], me_id)
            # not scoped to the named reviewers list anymore (owner,
            # 2026-07-16: this channel has more than 3 contributors - a
            # colleague's own review request got silently dropped by this
            # exact gate). Any colleague's genuine review request counts; the
            # classifier below is what tells a real ask apart from noise.
            # 2026-07-20: a sender allowlist was re-added here for a while to
            # scope to the team's own senders, but it caused the same class of
            # bug again - an untagged "up for review" from anyone outside the
            # hardcoded name list got dropped before the repo was ever
            # checked, even when the PR was the main repo itself
            # (owner, 2026-08-19: people stopped tagging CoS and those posts
            # went unreviewed). The repo allowlist below is a per-PR-URL gate
            # that already captures "is this our work" more precisely than
            # sender identity ever did, so it's now the sole scoping signal
            # for untagged messages.
            raw_body = (m.get("body") or {}).get("content", "") or ""
            body = graph.strip_html(raw_body)
            if not body.strip():
                continue
            # PR links usually render as a hyperlink with the PR number as the
            # visible text (e.g. anchor text "761", the URL only in href) - the
            # regex must run on the RAW html, not the stripped text, or the URL
            # is lost (owner, 2026-07-14: this was silently dropping every real
            # "up for review" post).
            urls = list(dict.fromkeys(u.lower() for u in PR_URL_RE.findall(raw_body)))
            if not tagged:
                urls = [u for u in urls if _repo_allowed(u, cfg)]
            if not urls or not await _is_review_request(body):
                continue
            already = {r["pr_url"] for r in db.query(
                "SELECT pr_url FROM pr_thread WHERE channel_id=? AND root_message_id=?",
                (channel_id, message_id))}
            urls = [u for u in urls if u not in already]
            if not urls:
                continue
            # a url already attempted before AND reached a terminal outcome -
            # successful (pr_thread, caught above) or dismissed (e.g. already
            # closed/merged) - must never be retried every tick forever (owner,
            # 2026-07-14: this was the "same reaction in a non-stop loop" bug - a
            # permanently-failed review had no record outside pr_thread, so it
            # looked brand new every pass). Status='new' means upsert_item
            # created the row but a crash interrupted the review before it
            # finished (2026-07-16: a batch crash left 4 PRs stuck at 'new'
            # forever under the old existence-only check) - those must retry.
            placeholders = ",".join("?" * len(urls))
            already_attempted = {r["external_id"] for r in db.query(
                f"SELECT external_id FROM items WHERE source='pr_channel_review' "
                f"AND status IN ('done','dismissed') AND external_id IN ({placeholders})",
                tuple(urls))}
            urls = [u for u in urls if u not in already_attempted]
            if not urls:
                continue

            await _react(team_id, channel_id, message_id, _REACT_SEEN)
            reviewed = []
            transient = []    # couldn't LOOK at the PR - retry, never a verdict
            unsubmitted = []  # reviewed, but the GH review submission never landed
            for url in urls:
                item_id = None
                try:
                    item_id, _ = db.upsert_item(
                        source="pr_channel_review", type_="pr_review", external_id=url,
                        conversation_id=f"{team_id}|{channel_id}", sender=author_name,
                        subject=f"review request from {author_name}", content=url,
                        received_at=(m.get("createdDateTime") or "").replace("Z", "+00:00"))
                    result = await _review_pr(url, cfg)
                    if not result:
                        # gh couldn't fetch it: GitHub 503, rate limit, network
                        # blip. That is TRANSIENT, so status must stay 'new' -
                        # 'dismissed' is terminal in already_attempted and the
                        # PR would never be looked at again (owner, 2026-08-17:
                        # a 2-second GitHub 503 permanently killed the reviews
                        # for #961 and #962, and CoS could not retry either).
                        db.update_item(item_id, action_taken="review fetch failed, retrying")
                        transient.append(url)
                        continue
                    if result.get("out_of_scope"):
                        db.update_item(item_id, status="dismissed", action_taken=result["out_of_scope"])
                        continue
                    comment = _findings_comment(result)
                    if comment:
                        await actions.post_github_comment(url, comment, item_id=item_id)
                    if not await _submit_gh_review(url, result, bool(comment), item_id):
                        unsubmitted.append(url)
                    # INSERT OR IGNORE: a concurrent sweep (e.g. an overlapping
                    # manual trigger) can race this exact insert - never let one
                    # PR's duplicate-key collision crash the whole batch and
                    # strand the rest of the PRs in this message unprocessed
                    # (owner, 2026-07-16).
                    db.execute(
                        "INSERT OR IGNORE INTO pr_thread(item_id, team_id, channel_id, "
                        "root_message_id, pr_url, author_name, last_reviewed_sha, "
                        "reply_cursor, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        # reply_cursor=None, not db.now(): a thread just tracked here
                        # hasn't SEEN any replies yet, so nothing should be treated as
                        # "already handled" until _phase_followups actually processes
                        # one (matches the other INSERT in _phase_followups - owner,
                        # 2026-07-21, spotted this inconsistency while chasing why
                        # a teammate's "back to you" ping on PR #834 got a silent checkmark).
                        (item_id, team_id, channel_id, message_id, url, author_name,
                         result.get("head_sha"), None, db.now(), db.now()))
                    db.update_item(item_id, status="done",
                                   action_taken=f"reviewed ({result.get('verdict')})")
                    db.audit("pr_channel_reviewed",
                             {"pr_url": url, "verdict": result.get("verdict"),
                              "channel_id": channel_id, "message_id": message_id}, item_id=item_id)
                    reviewed.append((url, result))
                except Exception as e:
                    # leave status='new' (from upsert_item) rather than
                    # 'dismissed' - this was a transient failure (timeout,
                    # network blip), not a permanent one like "already closed",
                    # and 'dismissed' would make already_attempted's status
                    # filter treat it as done-forever and never retry it
                    # (owner, 2026-07-16: exactly this swallowed 2 real PRs after
                    # a timeout during an overlapping-run pileup).
                    log.exception("pr_channel_review: processing %s failed", url)
                    if item_id is not None:
                        db.update_item(item_id, action_taken=f"retrying after error: {str(e)[:150]}")

            if not reviewed:
                if transient:
                    # CoS never got to look at these - leave the 👀 standing so
                    # the thread reads as "in progress" and retry next tick. ❌
                    # here is a lie: in this channel it means "this PR has
                    # problems", and everyone tagged reads it that way (owner,
                    # 2026-08-17, on #961/#962 getting a red X during a GitHub
                    # outage). Never report a verdict CoS didn't reach.
                    log.info("pr_channel_review: %d PR(s) unfetchable on %s, retrying "
                             "next tick", len(transient), message_id)
                    continue
                # every PR was already closed/merged - nothing left to review;
                # red X flags it as unresolved rather than silently done
                await _react(team_id, channel_id, message_id, _REACT_BLOCKED)
                continue
            reply = await _compose_reply(
                f"# Colleague to @mention (exact full display name)\n{author_name}\n\n"
                f"# Situation\nFirst pass review, asked by {author_name}.\n\n"
                f"# Review results\n"
                + json.dumps([_review_brief(u, r) for u, r in reviewed], ensure_ascii=False),
                cfg.get("model", "claude-sonnet-4-6"))
            if reply and _looks_like_meta_question(reply):
                log.warning("pr_channel_review: discarding a meta/clarifying reply "
                           "instead of posting it: %r", reply[:200])
                reply = ""
            if not reply:
                # a first-pass ack must never go silent after promising a reply
                parts = [f"@{author_name}, took a first pass:" if author_name
                         else "Took a first pass:"]
                for url, r in reviewed:
                    pr_num = re.search(r"/pull/(\d+)", url)
                    num = f"#{pr_num.group(1)}" if pr_num else url
                    verdict = r.get("verdict", "approved" if not r.get("critical") else "needs_changes")
                    word = "approved" if verdict == "approved" else f"needs changes ({len(r.get('critical') or [])} blocking)"
                    parts.append(f"- {num}: {word}")
                reply = "\n".join(parts)
            if unsubmitted:
                # never let the channel read "approved" when GitHub has no such
                # review on the PR (owner, 2026-08-17)
                reply = f"{reply}\n\n{_unsubmitted_note(unsubmitted)}"
            emoji = policy().get("chieff", {}).get("emoji", "🤖")
            posted = await actions.assistant_channel_post(
                team_id, channel_id, _md_to_html(f"{emoji} {reply}"), kind="pr_channel_reply",
                item_id=None, reply_to=message_id, attribute=False)
            has_blocking = any((r.get("critical") or []) for _, r in reviewed)
            await _react(team_id, channel_id, message_id,
                        _REACT_BLOCKED if has_blocking else _REACT_OK)
            # this thread may already have earlier @mentions from before CoS ever
            # looked at it (e.g. "back to you on PR 760") - they asked too, so
            # they get the same checkmark, matched to that specific PR's verdict
            try:
                existing_replies = await graph.read_channel_message_replies(
                    team_id, channel_id, message_id, top=50)
            except Exception:
                existing_replies = []
            verdict_by_url = {u: bool(r.get("critical")) for u, r in reviewed}
            for rep in existing_replies:
                rep_who = (rep.get("from") or {}).get("user") or {}
                if rep_who.get("id") == me_id:
                    continue
                rep_raw = (rep.get("body") or {}).get("content", "") or ""
                if not (_mentions_me(rep.get("mentions") or [], me_id)
                        or _RECHECK_RE.search(graph.strip_html(rep_raw))):
                    continue
                rep_urls = list(dict.fromkeys(u.lower() for u in PR_URL_RE.findall(rep_raw)))
                blocked = any(verdict_by_url.get(u) for u in rep_urls) if rep_urls else has_blocking
                if rep.get("id"):
                    await _react(team_id, channel_id, rep["id"],
                                _REACT_BLOCKED if blocked else _REACT_OK, item_id=None)
            results.append({"message_id": message_id, "author": author_name,
                            "prs": [u for u, _ in reviewed], "reply": posted})
        except Exception:
            # any single message's failure (classify timeout, a fresh
            # bug, anything) must never abort the rest of the batch -
            # this exact gap crashed a whole sweep mid-way through a
            # 9-PR message and stranded the untouched ones (owner,
            # 2026-07-16).
            log.exception("pr_channel_review: message %s failed", message_id)
    return results


def _mentions_me(mentions: list, me_id: str) -> bool:
    return any((mm.get("mentioned", {}).get("user") or {}).get("id") == me_id
               for mm in (mentions or []))


async def _phase_followups(team_id: str, channel_id: str, messages: list[dict],
                           cfg: dict, me_id: str) -> list[dict]:
    """Continue conversations in ANY PR thread in the channel (not just ones
    this automation started, e.g. the owner's own manual review threads), but
    only when the owner is explicitly @mentioned - never on a plain reply, even
    from the PR's author, since CoS shouldn't jump into a conversation the owner
    is already having himself unless summoned (owner, 2026-07-14).

    The PR link is often only in the REPLY itself ("back to you on PR <link>"),
    not the thread's root post (which may just say "#760" in plain text) - so
    every top-level thread in the window is checked for a fresh @mention,
    not just ones whose root happens to carry a recognizable PR link."""
    results = []
    tracked_by_root: dict[str, list[dict]] = {}
    for r in db.query("SELECT * FROM pr_thread WHERE status='active' AND channel_id=?",
                      (channel_id,)):
        tracked_by_root.setdefault(r["root_message_id"], []).append(dict(r))

    root_bodies_raw: dict[str, str] = {}
    root_ids: set[str] = set(tracked_by_root)
    for m in messages:
        if m.get("replyToId"):
            continue
        message_id = m.get("id")
        if not message_id:
            continue
        root_ids.add(message_id)
        root_bodies_raw[message_id] = (m.get("body") or {}).get("content", "") or ""

    for root_message_id in root_ids:
        try:
            replies = await graph.read_channel_message_replies(
                team_id, channel_id, root_message_id, top=50)
        except Exception as e:
            log.warning("pr_channel_review: failed to read replies for %s: %s",
                       root_message_id, e)
            continue
        if not replies:
            continue

        tracked_urls = {r["pr_url"]: r for r in tracked_by_root.get(root_message_id, [])}
        root_urls = list(dict.fromkeys(
            u.lower() for u in PR_URL_RE.findall(root_bodies_raw.get(root_message_id, ""))))

        fresh_by_url: dict[str, list[dict]] = {}
        for rep in replies:
            who = (rep.get("from") or {}).get("user") or {}
            if who.get("id") == me_id:
                continue  # never react to the owner's/CoS's own posts (same identity)
            rep_raw = (rep.get("body") or {}).get("content", "") or ""
            body = graph.strip_html(rep_raw)
            if not body.strip():
                continue
            # addressed = an explicit @mention, or phrasing that reads as
            # asking for another look ("back to you", "can you check") even
            # without a tag - a plain unaddressed reply still gets no response
            is_mention = _mentions_me(rep.get("mentions") or [], me_id)
            if not (is_mention or _RECHECK_RE.search(body)):
                continue
            at = (rep.get("createdDateTime") or "").replace("Z", "+00:00")
            # which PR is this about? prefer a link in the mention itself,
            # then fall back to whatever this thread is already tracking or
            # what the root post links to - but ONLY when that's unambiguous
            # (exactly one candidate). A thread can end up tracking more than
            # one PR (e.g. a sibling PR mentioned as "related context" gets
            # mis-bootstrapped into the same thread), and blasting an
            # untagged reply to ALL of them cross-contaminates unrelated PRs -
            # this is exactly how PR #843 got re-reviewed and its "approved"
            # comment reposted 4x, purely because replies about a SIBLING PR
            # in the same thread used words like "fixed"/"pushed" that also
            # matched #843's recheck trigger (owner, 2026-07-23). With 2+
            # candidates and no explicit link, skip rather than guess.
            urls = list(dict.fromkeys(u.lower() for u in PR_URL_RE.findall(rep_raw)))
            if not urls:
                candidates = list(tracked_urls) or root_urls
                urls = candidates if len(candidates) == 1 else []
            if not urls:
                continue  # can't tell which PR this reply is about
            entry = {"who": who.get("displayName") or "", "at": at,
                     "text": body[:1000], "id": rep.get("id"), "mentioned": is_mention}
            for url in urls:
                cursor = (tracked_urls.get(url) or {}).get("reply_cursor") or ""
                if cursor and at <= cursor:
                    continue
                fresh_by_url.setdefault(url, []).append(entry)

        for url, fresh in fresh_by_url.items():
            try:
                row = tracked_urls.get(url)
                if row is None:
                    # a brand-new engagement (never tracked before) is gated
                    # by the same repo allowlist as _phase_new_reviews (owner,
                    # 2026-07-20) - the PR must be in-scope. A thread ALREADY
                    # tracked (row is not None) skips this - once legitimately
                    # started, replies from anyone in that thread still get
                    # answered. An explicit @mention of the owner overrides the
                    # repo gate, same reasoning as _phase_new_reviews (owner,
                    # 2026-07-22: "BUT JUST IF THEY TAG ME"). The sender
                    # allowlist (_author_allowed) used to gate this too, but
                    # that dropped untagged replies from anyone outside the
                    # hardcoded name list even on an in-scope PR - repo scope
                    # alone is the signal now (owner, 2026-08-19).
                    tagged = any(f.get("mentioned") for f in fresh)
                    if not tagged and not _repo_allowed(url, cfg):
                        continue
                    # this PR may already be tracked/attempted under a DIFFERENT
                    # root in this channel (e.g. the same PR gets linked again in
                    # a later "up for review" post) - tracked_urls only covers
                    # THIS root, so check globally before starting a duplicate
                    # thread and re-reviewing something already handled (owner,
                    # 2026-07-14: this produced a second, independent review reply
                    # for a PR already covered elsewhere).
                    if db.query("SELECT 1 FROM pr_thread WHERE channel_id=? AND pr_url=? LIMIT 1",
                               (channel_id, url)):
                        continue
                    if db.query("SELECT 1 FROM items WHERE source='pr_channel_review' "
                               "AND status IN ('done','dismissed') AND external_id=? LIMIT 1",
                               (url,)):
                        continue
                    # first time CoS has been summoned into this thread - start
                    # tracking it (last_reviewed_sha=None forces a fresh review
                    # below, since CoS never checked this PR itself before)
                    item_id, _ = db.upsert_item(
                        source="pr_channel_review", type_="pr_review", external_id=url,
                        conversation_id=f"{team_id}|{channel_id}", content=url)
                    # INSERT OR IGNORE: see the matching note in
                    # _phase_new_reviews - a concurrent sweep can race this
                    # exact insert, and one PR's collision must never crash
                    # the whole batch.
                    db.execute(
                        "INSERT OR IGNORE INTO pr_thread(item_id, team_id, channel_id, "
                        "root_message_id, pr_url, author_name, last_reviewed_sha, "
                        "reply_cursor, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (item_id, team_id, channel_id, root_message_id, url, fresh[0]["who"],
                         None, None, db.now(), db.now()))
                    tracked_row = db.query(
                        "SELECT id FROM pr_thread WHERE channel_id=? AND root_message_id=? "
                        "AND pr_url=?", (channel_id, root_message_id, url))
                    if not tracked_row:
                        continue  # lost the race to a concurrent sweep; skip this tick
                    row = {"id": tracked_row[0]["id"], "item_id": item_id, "pr_url": url,
                          "last_reviewed_sha": None}

                async def _react_fresh(emoji: str) -> None:
                    for f in fresh:
                        if f["id"]:
                            await _react(team_id, channel_id, f["id"], emoji, item_id=row["item_id"])

                async def _ack_closed(reason: str) -> None:
                    # Owner, 2026-07-21: a merged/closed PR used to short-circuit
                    # here with only a checkmark reaction and no reply - to a
                    # message that explicitly @mentioned or "back to you"'d
                    # the owner, a bare reaction reads as "CoS didn't do anything"
                    # (easy to miss among other checkmarks in the thread).
                    # `reason` is a complete clause, e.g. "PR already merged".
                    tags = " ".join(f"@{n}" for n in dict.fromkeys(
                        f["who"] for f in fresh if f["who"]))
                    emoji = policy().get("chieff", {}).get("emoji", "🤖")
                    msg = (f"{tags} - " if tags else "") + f"{reason}, nothing further to review here."
                    try:
                        await actions.assistant_channel_post(
                            team_id, channel_id, _md_to_html(f"{emoji} {msg}"),
                            kind="pr_channel_reply", item_id=row["item_id"],
                            reply_to=root_message_id, attribute=False)
                    except Exception:
                        log.warning("pr_channel_review: closed-PR ack post failed for %s",
                                   row["pr_url"], exc_info=True)

                await _react_fresh(_REACT_SEEN)
                fresh.sort(key=lambda x: x["at"])
                newest_ts = fresh[-1]["at"]
                reply_text = " ".join(f["text"] for f in fresh)

                rc, state_raw = await _gh("pr", "view", row["pr_url"], "--json", "state,headRefOid")
                if rc != 0:
                    # transient fetch failure, same reasoning as _phase_new_reviews:
                    # keep 👀, don't claim a verdict, and leave reply_cursor alone
                    # so this reply is picked up again next tick
                    await _react_fresh(_REACT_SEEN)
                    continue
                state_meta = json.loads(state_raw)
                if (state_meta.get("state") or "").upper() != "OPEN":
                    db.execute("UPDATE pr_thread SET status='closed', updated_at=? WHERE id=?",
                              (db.now(), row["id"]))
                    await _ack_closed(f"PR already {(state_meta.get('state') or 'closed').lower()}")
                    await _react_fresh(_REACT_OK)
                    continue
                new_sha = state_meta.get("headRefOid")
                sha_changed = new_sha != row["last_reviewed_sha"]
                recheck = sha_changed or bool(_RECHECK_RE.search(reply_text))
                # A phrase-match recheck ("fixed", "pushed", "back to you", ...)
                # on a commit ALREADY reviewed re-diffs identical code and
                # produces an identical verdict every time - reposting it is
                # spam, not new information. Only do the expensive re-review
                # (and the GH comment/formal review it posts) when the code
                # itself changed; a phrase-only match still gets a normal
                # conversational reply below, just without re-reviewing
                # (owner, 2026-07-23 - the direct fix for PR #843's 4x-posted
                # "approved" comment; the misattribution fix above should
                # prevent the underlying false trigger too, but this is a
                # backstop against any other path that re-fires a recheck
                # for a commit that hasn't actually changed).
                already_reviewed_this_sha = bool(row["last_reviewed_sha"]) and not sha_changed

                findings_ctx = ""
                has_blocking = False
                review_failed = False
                if recheck and not already_reviewed_this_sha:
                    result = await _review_pr(row["pr_url"], cfg)
                    if result and result.get("out_of_scope"):
                        db.execute("UPDATE pr_thread SET status='closed', updated_at=? WHERE id=?",
                                  (db.now(), row["id"]))
                        await _ack_closed(result["out_of_scope"])
                        await _react_fresh(_REACT_OK)
                        continue
                    if result:
                        comment = _findings_comment(result)
                        if comment:
                            await actions.post_github_comment(row["pr_url"], comment,
                                                              item_id=row["item_id"])
                        await _submit_gh_review(row["pr_url"], result, bool(comment),
                                               row["item_id"])
                        db.execute("UPDATE pr_thread SET last_reviewed_sha=?, updated_at=? WHERE id=?",
                                  (result.get("head_sha"), db.now(), row["id"]))
                        findings_ctx = json.dumps(_review_brief(row["pr_url"], result),
                                                  ensure_ascii=False)
                        has_blocking = bool(result.get("critical"))
                    else:
                        review_failed = True

                reply = await _compose_reply(
                    f"# PR\n{row['pr_url']}\n\n"
                    + (f"# Fresh review findings (just re-checked the current diff)\n{findings_ctx}\n\n"
                       if findings_ctx else
                       "# Note\nNo new commits since the last review - this is a conversation, "
                       "not a re-check.\n\n")
                    + "# Thread replies to respond to\n" + json.dumps(fresh, ensure_ascii=False),
                    cfg.get("model", "claude-sonnet-4-6"))
                if reply and _looks_like_meta_question(reply):
                    log.warning("pr_channel_review: discarding a meta/clarifying reply "
                               "instead of posting it: %r", reply[:200])
                    reply = ""
                if reply:
                    emoji = policy().get("chieff", {}).get("emoji", "🤖")
                    posted = await actions.assistant_channel_post(
                        team_id, channel_id, _md_to_html(f"{emoji} {reply}"), kind="pr_channel_reply",
                        item_id=row["item_id"], reply_to=root_message_id, attribute=False)
                    db.audit("pr_channel_followup",
                             {"pr_url": row["pr_url"], "recheck": recheck, "reply": reply},
                             item_id=row["item_id"])
                    results.append({"pr_url": row["pr_url"], "recheck": recheck, "posted": posted})
                # review_failed is CoS's problem, not the PR's - it gets 👀 (still
                # working on it), never ❌ (owner, 2026-08-17)
                if review_failed and not has_blocking:
                    await _react_fresh(_REACT_SEEN)
                else:
                    await _react_fresh(_REACT_BLOCKED if has_blocking else _REACT_OK)
                db.execute("UPDATE pr_thread SET reply_cursor=?, updated_at=? WHERE id=?",
                          (newest_ts, db.now(), row["id"]))
            except Exception:
                log.exception("pr_channel_review: followup processing %s failed", url)
    return results


_SWEEP_LOCK = asyncio.Lock()


async def sweep() -> dict:
    if paused():
        return {"skipped": "pr_channel_review paused"}
    # A manual trigger (POST /api/sweep/pr_channel_review) bypasses the
    # scheduler's own max_instances=1 tracking entirely, so a scheduled tick
    # can start while a manual run (or another scheduled one, if a review
    # runs longer than the 5-min interval) is still mid-flight. Reviews take
    # real minutes; two overlapping runs would redundantly re-review the same
    # PRs and could each post their own reply for the same batch (owner,
    # 2026-07-16). A non-blocking lock means the second one just skips this
    # tick instead of racing the first.
    if _SWEEP_LOCK.locked():
        return {"skipped": "a pr_channel_review sweep is already running"}
    async with _SWEEP_LOCK:
        cfg = _cfg()
        team_id = cfg.get("team_id")
        channel_id = cfg.get("channel_id")
        if not team_id or not channel_id:
            return {"skipped": "no pr_channel_review.team_id/channel_id configured"}
        if not actions.read_allowed(actions.channel_key(team_id, channel_id)):
            return {"skipped": "reading disabled for this channel (Write Access board)"}
        me_id = policy()["me"]["aad_id"]

        messages = await graph.read_channel_messages(team_id, channel_id, top=50)
        new_reviews = await _phase_new_reviews(team_id, channel_id, messages, cfg, me_id)
        followups = await _phase_followups(team_id, channel_id, messages, cfg, me_id)
        return {"new_reviews": len(new_reviews), "followups": len(followups)}
