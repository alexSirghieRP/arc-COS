"""Daily scrum report: a 150-220 word, copyable-and-editable "what I did"
summary of the user's own work, scoped mostly to the configured repo.

Runs each weekday at 16:00 server-local time (matches the timestamps in the
scheduler log). Window is a rolling 24h ending
at the 16:30 boundary: "yesterday 16:30 -> today 16:30" normally, or "Friday
16:30 -> Monday 16:30" (through the weekend) on a Monday, so both today's
same-day work and nothing from Fri/Sat/Sun falls through the cracks (owner
decision, 2026-07-21, refined 2026-07-22 to the 16:30 rolling boundary per the
owner's "yesterday's 16:30 to today's 16:30" spec).

Evidence gathered per run:
- GitHub: PRs authored by the user + commits (via the GitHub commit-search API),
  scoped to the configured repo, plus each PR's CI status checks (a
  test pass/fail signal, when there is one). "Branches made" is approximated
  by each PR's head ref - GitHub has no cheap "branches created in the last
  day" endpoint, and a branch worth reporting almost always has a PR anyway.
- GitHub: PRs the user (via CoS) reviewed FOR colleagues in the pr_channel_review
  automation - pulled from our own pr_thread/items tables, which already
  carry the requester's name and verdict, so those bullets can say "reviewed
  for Jane Doe" instead of just listing a PR number.
- ADO: work items the user changed in the window (ado.my_recent_changes), scoped
  to the configured ADO projects.
- Confluence: pages the user edited/created, filtered to the window (reuses
  confluence.my_updates(), whose own lookback is broader/config-driven).
- Claude Code sessions: local *.jsonl transcripts under
  ~/.claude/projects/**/ whose directory name references
  the configured repo and whose mtime falls in the window - a rough
  "what Claude Code helped with" signal, best-effort only (local file
  scanning, not an API - if the on-disk layout ever changes this quietly
  returns nothing rather than erroring the whole report).

The result is short structured markdown (bold lead + '- ' bullets grouped by
source, with links and named collaborators), persisted one row per calendar
day (upsert). The user's edits in the Scrum tab are never clobbered by a later
regenerate - only the "reset to" baseline (generated_text) updates once a
ticket has been hand-edited.
"""

import asyncio
import json
import logging
import re
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

from .. import ado, confluence, db
from ..agents import CHIEF, run_agent
from ..config import policy

# The daily boundary the owner wants: "yesterday's 16:30 to today's 16:30"
# (owner decision, 2026-07-22) - a rolling window ending mid-afternoon, not midnight, so the
# 16:00 generation run captures today's morning/early-afternoon work too.
_BOUNDARY = dtime(16, 30)

log = logging.getLogger("chief.sweep.scrum")

# Repo scoping comes from policy.yaml: scrum.repo (repo name) + github.owner (org).
REPO = (policy().get("scrum", {}) or {}).get("repo", "example-repo")
REPO_FULL = f"{(policy().get('github', {}) or {}).get('owner', 'your-github-org')}/{REPO}"

SCRUM_SCHEMA = {
    "type": "object",
    "properties": {
        "summary_md": {
            "type": "string",
            "description": "150-220 word scrum update in first person, formatted as short "
                            "markdown: one bold one-line bottom-line lead, then '- ' bullets "
                            "grouped under '**GitHub**' / '**ADO**' / '**Confluence**' headers "
                            "(only for sections with real content), one bullet per PR/work "
                            "item/page carrying the SAME status emoji as its evidence line "
                            "(✅/🆕/🔄/⚠️/❌/🗑️/🔁 - never invented; 🔁 = reopened/regressed back "
                            "to rework, always wins over any other emoji for that item), an "
                            "inline markdown link ([PR #123](url), [AB#456](url)), any "
                            "test-case number and assignee the ADO evidence carries, and - "
                            "where the evidence shows a collaborator (reviewer, co-author, "
                            "requester) - their name on that bullet (e.g. 'reviewed for Jane "
                            "Doe', 'with John Smith'). If any evidence item is "
                            "reopened, ALWAYS add a mandatory '**Reopened**' section (right "
                            "after ADO) calling it out as a risk regression - never fold it "
                            "quietly into the ADO bullets or omit it. ALWAYS end with a "
                            "mandatory '**Focus next**' section: a one-line priority statement "
                            "plus terse bullets naming the still-open UAT backlog items "
                            "(test-case number/AB# + link + assignee) the user has not yet "
                            "resolved, and a blocked-bullet only if the evidence shows a real "
                            "blocker.",
        },
    },
    "required": ["summary_md"],
}

PROMPT = """Write the user's daily scrum/standup update: what he personally did
{period_label}, mostly on the {repo} codebase.

Rules:
- STRICT 150-220 words total. Count matters - a report outside that range is unusable.
- Format as short markdown, not one paragraph:
  - One bold one-line bottom-line lead sentence first.
  - Then '- ' bullets grouped under a '**GitHub**' / '**ADO**' / '**Confluence**'
    bold header per section - only include a section header if that source has
    real content; skip empty sections entirely.
  - One bullet per concrete PR/commit/work item/page, first person voice
    within each bullet (e.g. "- Shipped ..." / "- Reviewed ... for Jane Doe").
  - Blank line between sections for readability when pasted.
  - If the REOPENED evidence below is non-empty, add a '**Reopened**' section
    right after ADO (before Focus next) - this is mandatory whenever that
    evidence is non-empty, never optional or foldable into the ADO bullets.
    One bullet per reopened item: test-case number/AB# with a link, its
    assignee, and that it regressed back to rework (not just "still open").
    This is a risk regression the user tracks closely - never soften or omit it.
    Omit this section entirely only when the REOPENED evidence says "(none)".
  - ALWAYS end with a '**Focus next**' section (bold header, then bullets) -
    this is not optional like the other sections. Base it on the "still open"
    evidence below: name what the user will prioritize next, then list the
    remaining open items he has NOT yet resolved (by test-case number/AB# with
    a link), each with its assignee if not the user. Keep this section tight - a
    one-line priority statement plus terse bullets, not full sentences per
    item.
- Every PR/ADO/commit evidence line below already starts with a status emoji
  (✅ done/merged/closed, 🆕 new/not-yet-started, 🔄 actively in progress/open,
  ⚠️ open bug or failing checks, ❌ closed without merging, 🗑️ removed/cancelled,
  🔁 reopened/regressed back to rework - this one always wins over any other
  emoji for that item). Copy that SAME emoji onto the bullet you write
  for it - do not invent a different one, drop it, or guess a status the
  emoji doesn't already show.
- Reference concrete PRs/ADO items by number with an inline markdown link
  wherever the evidence gives a URL, e.g. "[PR #821](https://...)".
- Name the collaborator on a bullet whenever the evidence shows one (a PR
  author you reviewed for, someone who requested/approved/co-authored,
  an ADO assignee) - e.g. "reviewed for Jane Doe", "with John Smith".
  Never invent a name that isn't in the evidence.
- Every ADO evidence line carries "assigned: <name>" and, if present, a
  test-case number in parentheses (e.g. "(TC-UAT-029)") - include both the
  test-case number and the assignee on that item's bullet, e.g. "⚠️
  TC-UAT-029 [AB#456](url), assigned to Jane Doe, still open: <title>".
- Mention test/CI status only if the evidence actually shows it (pass/fail
  counts) - never invent test results.
- CLAUDE CODE SESSIONS ARE NOT EVIDENCE OF STATUS OR COMPLETION. A session's
  first prompt is only what the user TYPED to Claude at the time - it may be a
  plan, a question, or work that was later abandoned, superseded, or already
  finished/closed by the time this report runs. Never create a bullet from a
  session alone, and never use one to claim something is done, in progress,
  or "still being worked on". Only use a session's first prompt as extra
  color folded onto a bullet that ALREADY exists from PR/ADO/Confluence
  evidence (same PR/ADO number appears in both) - and even then, never let
  it override that item's actual state/emoji from the ADO or GitHub evidence.
- Ground every claim in the evidence below. If evidence is thin for a
  section, say less rather than padding or inventing detail.
- Within "**Focus next**", add a "- ⚠️ Blocked:" bullet only if the evidence
  itself shows a real blocker.

=== GITHUB: PRs by the user ({repo}) ===
{prs}

=== GITHUB: commits by the user ({repo}) ===
{commits}

=== GITHUB: PRs the user reviewed for colleagues (via CoS in the PR Reviews channel) ===
{reviews_done}

=== ADO: work items the user changed ===
{ado}

=== ADO: full remaining open UAT backlog (everyone's, not just the user's - use
this for the mandatory "Focus next" section; items also listed above are
ones the user already touched in this window, so frame those as progress, not as
still-to-do) ===
{uat_open}

=== ADO: REOPENED this window (regressed from resolved/closed back to rework -
a real risk regression, not routine progress) ===
{reopened}

=== CONFLUENCE: pages the user touched ===
{confluence}

=== CLAUDE CODE SESSIONS touching {repo} ===
{sessions}
"""


def _cfg() -> dict:
    return policy().get("scrum", {}) or {}


def _window(today: date) -> dict:
    """Normally "yesterday 16:30 -> today 16:30" (both same-day-so-far work
    and yesterday's evening are covered by a 16:00 generation run). On
    Monday, back up to last Friday 16:30 so Fri/Sat/Sun work isn't dropped
    (owner decision, 2026-07-21/22)."""
    if today.weekday() == 0:  # Monday
        start_date = today - timedelta(days=3)
        period_label = "since Friday afternoon (including the weekend)"
    else:
        start_date = today - timedelta(days=1)
        period_label = "since yesterday afternoon"
    start = datetime.combine(start_date, _BOUNDARY)
    end = datetime.combine(today, _BOUNDARY)
    yesterday = today - timedelta(days=1)
    date_range = (f"{start_date:%b %d}" if start_date == yesterday
                  else f"{start_date:%b %d}–{yesterday:%b %d}")
    return {"start": start, "end": end, "start_date": start_date,
            "period_label": period_label, "date_range": date_range}


async def _gh_json(*args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "gh", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    return proc.returncode, (out.decode() if proc.returncode == 0 else err.decode())


def _since_iso(win: dict) -> str:
    """win['start'] is a naive server-local datetime; GitHub's search
    qualifiers want a precise UTC instant. Datetime.astimezone() on a naive
    value assumes system-local time, which is correct here since the window
    is built in the server's local time (owner decision, 2026-07-22: needed once the window
    boundary moved off midnight - a date-only "since" would re-include the
    first 16.5 hours of start_date that yesterday's report already covered)."""
    return win["start"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


async def _gather_prs(win: dict, login: str) -> list[dict]:
    since = _since_iso(win)
    rc, out = await _gh_json(
        "pr", "list", "--repo", REPO_FULL, "--author", login, "--state", "all",
        "--search", f"updated:>={since}", "--limit", "50",
        "--json", "number,title,url,state,createdAt,mergedAt,updatedAt,headRefName,"
                  "statusCheckRollup")
    if rc != 0:
        log.warning("scrum: gh pr list failed: %s", out[:200])
        return []
    try:
        return json.loads(out)
    except ValueError:
        return []


async def _gather_commits(win: dict, login: str) -> list[dict]:
    since = _since_iso(win)
    rc, out = await _gh_json(
        "api", "search/commits",
        "-f", f"q=repo:{REPO_FULL} author:{login} author-date:>={since}",
        "-f", "sort=author-date", "-f", "order=desc", "-f", "per_page=30")
    if rc != 0:
        log.warning("scrum: gh commit search failed: %s", out[:200])
        return []
    try:
        data = json.loads(out)
    except ValueError:
        return []
    items = []
    for c in data.get("items", []):
        commit = c.get("commit", {})
        items.append({
            "sha": (c.get("sha") or "")[:8],
            "message": (commit.get("message") or "").split("\n")[0][:150],
            "url": c.get("html_url"),
            "date": (commit.get("author") or {}).get("date"),
        })
    return items


def _gather_reviews_done(win: dict) -> list[dict]:
    """PRs the user (via CoS) reviewed for colleagues in the window - joins
    pr_thread (requester's name) with items (verdict), so the "owner" of
    each reviewed PR shows up in the report (owner note, 2026-07-22)."""
    rows = db.query(
        "SELECT t.pr_url, t.author_name, i.action_taken "
        "FROM pr_thread t JOIN items i ON i.id = t.item_id "
        "WHERE t.updated_at >= ? AND t.updated_at < ? AND i.source='pr_channel_review'",
        (win["start"].isoformat(), win["end"].isoformat()))
    return [dict(r) for r in rows]


_CLAUDE_PROJECTS_ROOT = Path.home() / ".claude" / "projects"


def _gather_claude_sessions(win: dict) -> list[dict]:
    """Best-effort: local Claude Code transcripts whose project dir mentions
    the repo and whose mtime falls in the window. Never raises - a change to
    Claude Code's on-disk layout should degrade this to an empty list, not
    break the whole report."""
    sessions: list[dict] = []
    try:
        if not _CLAUDE_PROJECTS_ROOT.is_dir():
            return []
        start_ts = win["start"].timestamp()
        end_ts = win["end"].timestamp()
        for proj_dir in _CLAUDE_PROJECTS_ROOT.iterdir():
            if REPO not in proj_dir.name:
                continue
            for f in proj_dir.glob("*.jsonl"):
                try:
                    mtime = f.stat().st_mtime
                except OSError:
                    continue
                if not (start_ts <= mtime < end_ts):
                    continue
                first_prompt = ""
                try:
                    with f.open(errors="ignore") as fh:
                        for _ in range(200):
                            line = fh.readline()
                            if not line:
                                break
                            try:
                                o = json.loads(line)
                            except ValueError:
                                continue
                            msg = o.get("message")
                            if o.get("type") == "user" and isinstance(msg, dict):
                                content = msg.get("content")
                                text = (content if isinstance(content, str) else next(
                                    (b.get("text", "") for b in content or []
                                     if isinstance(b, dict) and b.get("type") == "text"), ""))
                                if text.strip():
                                    first_prompt = text.strip()[:200]
                                    break
                except OSError:
                    continue
                sessions.append({
                    "project_dir": proj_dir.name,
                    "first_prompt": first_prompt,
                    "last_activity": datetime.fromtimestamp(mtime).isoformat(timespec="minutes"),
                })
    except Exception:
        log.warning("scrum: Claude session scan failed", exc_info=True)
        return []
    return sessions


# Deterministic status emojis, computed in code from the actual state data -
# never left to the LLM to guess, so a bullet's risk signal is always
# grounded in real evidence (owner decision, 2026-07-22: added after the model
# fabricated a status claim from a Claude Code session prompt instead of the
# real ADO state).
_TC_RE = re.compile(r"\bTC-UAT-\d+\b", re.I)
_DONE_WORDS = ("done", "closed", "resolved", "completed", "merged")
_DROPPED_WORDS = ("removed", "cancelled", "canceled")
# Not-yet-started states - distinct from "actively in progress" (🔄), so a
# fresh/unassigned task doesn't read as if work is already cycling on it
# (owner decision, 2026-07-22: a "new/unassigned" item got the wrong arrows icon).
_NEW_WORDS = ("new", "to do", "proposed", "backlog")


def _pr_emoji(p: dict) -> str:
    state = (p.get("state") or "").upper()
    if state == "MERGED":
        return "✅"
    if state == "CLOSED":
        return "❌"
    checks = p.get("statusCheckRollup") or []
    if checks and any((c.get("conclusion") or "").upper() not in ("SUCCESS", "NEUTRAL", "SKIPPED")
                      for c in checks):
        return "⚠️"
    return "🔄"


def _is_reopened(it: dict) -> bool:
    return (it.get("reason") or "").strip().lower() == "reopened"


def _ado_emoji(it: dict) -> str:
    # Checked first, ahead of state - a reopened item regressed from
    # resolved/closed back to rework, which is a bigger risk signal than its
    # current state alone would suggest (owner decision, 2026-07-23).
    if _is_reopened(it):
        return "🔁"
    state = (it.get("state") or "").lower()
    if any(k in state for k in _DONE_WORDS):
        return "✅"
    if any(k in state for k in _DROPPED_WORDS):
        return "🗑️"
    if any(k in state for k in _NEW_WORDS):
        return "🆕"
    if (it.get("type") or "").lower() in ("bug", "defect"):
        return "⚠️"  # an open bug already being worked is a live risk/gap
    return "🔄"


def _review_emoji(action_taken: str) -> str:
    at = (action_taken or "").lower()
    if "needs_changes" in at or "needs changes" in at:
        return "⚠️"
    if "approved" in at:
        return "✅"
    return "🔄"


def _fmt_prs(prs: list[dict]) -> str:
    if not prs:
        return "(none)"
    lines = []
    for p in prs:
        checks = p.get("statusCheckRollup") or []
        test_bits = ""
        if checks:
            passed = sum(1 for c in checks if (c.get("conclusion") or "").upper() == "SUCCESS")
            test_bits = f" [checks: {passed}/{len(checks)} passing]"
        lines.append(f"{_pr_emoji(p)} #{p['number']} [{p['state']}] {p['title']} - {p['url']}"
                     f" (branch {p.get('headRefName', '?')}){test_bits}")
    return "\n".join(lines)


def _fmt_commits(commits: list[dict]) -> str:
    if not commits:
        return "(none)"
    return "\n".join(f"{c['sha']} {c['message']} - {c.get('url', '')}" for c in commits)


def _fmt_reviews_done(items: list[dict]) -> str:
    if not items:
        return "(none)"
    lines = []
    for it in items:
        action = (it.get("action_taken") or "").strip()
        lines.append(f"{_review_emoji(action)} {it['pr_url']} - requested by "
                     f"{it.get('author_name') or '?'} - {action}")
    return "\n".join(lines)


def _fmt_ado(items: list[dict]) -> str:
    if not items:
        return "(none)"
    lines = []
    for it in items:
        tc = _TC_RE.search(it.get("title") or "")
        tc_bit = f" ({tc.group(0)})" if tc else ""
        assignee = it.get("assigned_to") or "unassigned"
        lines.append(f"{_ado_emoji(it)} {it['type']} #{it['id']}{tc_bit} [{it['state']}] "
                     f"assigned: {assignee} - {it['title']} - {it['url']}")
    return "\n".join(lines)


def _fmt_reopened(items: list[dict]) -> str:
    if not items:
        return "(none)"
    lines = []
    for it in items:
        tc = _TC_RE.search(it.get("title") or "")
        tc_bit = f" ({tc.group(0)})" if tc else ""
        assignee = it.get("assigned_to") or "unassigned"
        lines.append(f"🔁 {it['type']} #{it['id']}{tc_bit} [now {it['state']}] "
                     f"assigned: {assignee} - REOPENED on {it['changed']} (regressed back to "
                     f"rework) - {it['title']} - {it['url']}")
    return "\n".join(lines)


def _fmt_confluence(items: list[dict], win: dict) -> str:
    start_str = win["start"].strftime("%Y-%m-%d %H:%M")
    in_window = [i for i in items if (i.get("when") or "") >= start_str]
    if not in_window:
        return "(none)"
    return "\n".join(f"{i['title']} ({i['space']}) - {i['url']}" for i in in_window)


def _fmt_sessions(sessions: list[dict]) -> str:
    if not sessions:
        return "(none found - or Claude Code's session layout doesn't match what this looks for)"
    return "\n".join(f"- {s['last_activity']}: {s['first_prompt'] or '(no readable first prompt)'}"
                     for s in sessions)


async def generate(target: date | None = None) -> dict:
    """Build (or rebuild) today's scrum ticket. Idempotent per calendar day:
    regenerating replaces generated_text, but never summary_text once the user has
    hand-edited it (edited=1) - the edit is the source of truth from then on."""
    cfg = _cfg()
    today = target or date.today()
    win = _window(today)
    me_logins = policy().get("me", {}).get("github_logins") or []
    login = cfg.get("github_login") or (me_logins[0] if me_logins else "your-github-login")
    ado_projects = cfg.get("ado_projects")  # None -> ado.configured_projects()
    ado_days = (today - win["start_date"]).days + 1
    uat_filter = cfg.get("uat_title_filter", "TC-UAT")

    prs, commits, reviews_done, ado_items, uat_open, conf, sessions = await asyncio.gather(
        _gather_prs(win, login),
        _gather_commits(win, login),
        asyncio.to_thread(_gather_reviews_done, win),
        asyncio.to_thread(ado.my_recent_changes, ado_days, ado_projects),
        asyncio.to_thread(ado.open_items_matching, uat_filter, ado_projects),
        asyncio.to_thread(confluence.my_updates),
        asyncio.to_thread(_gather_claude_sessions, win),
    )
    conf_items = conf.get("items", []) if isinstance(conf, dict) else []
    # the backlog list overlaps with ado_items (things the user already touched) -
    # keep only what's genuinely still outstanding for the "Focus next"
    # section, so the model isn't tempted to double-list the same ticket.
    touched_ids = {it["id"] for it in ado_items}
    uat_still_open = [it for it in uat_open if it["id"] not in touched_ids]
    # items that regressed from resolved/closed back to rework THIS window -
    # a risk signal the owner explicitly wants surfaced (2026-07-23). Checked
    # across both gathered ADO sets (dedup by id) using the standard ADO
    # Reason field, which the workflow sets alongside State - no extra API
    # calls or revision-history lookups needed.
    start_date_str = win["start_date"].isoformat()
    reopened_by_id = {}
    for it in ado_items + uat_open:
        if _is_reopened(it) and it.get("changed", "") >= start_date_str:
            reopened_by_id[it["id"]] = it
    reopened = list(reopened_by_id.values())

    prompt = PROMPT.format(
        period_label=win["period_label"], repo=REPO,
        prs=_fmt_prs(prs), commits=_fmt_commits(commits),
        reviews_done=_fmt_reviews_done(reviews_done),
        ado=_fmt_ado(ado_items), uat_open=_fmt_ado(uat_still_open),
        reopened=_fmt_reopened(reopened),
        confluence=_fmt_confluence(conf_items, win),
        sessions=_fmt_sessions(sessions))
    result = await asyncio.wait_for(
        run_agent(CHIEF, prompt, schema=SCRUM_SCHEMA, with_tools=False, max_turns=6,
                  model=cfg.get("model", "claude-sonnet-4-6"), label="scrum"),
        timeout=int(cfg.get("timeout_seconds", 180)))
    if not isinstance(result, dict) or not result.get("summary_md"):
        raise RuntimeError("scrum synthesis produced no summary")

    summary_md = result["summary_md"].strip()
    evidence = json.dumps({"prs": prs, "commits": commits, "reviews_done": reviews_done,
                           "ado": ado_items, "uat_open": uat_still_open, "reopened": reopened,
                           "confluence": conf_items, "sessions": sessions},
                          ensure_ascii=False)
    report_date = today.isoformat()
    ts = db.now()
    existing = db.query("SELECT id, edited, summary_text FROM scrum_reports WHERE report_date=?",
                        (report_date,))
    if existing and existing[0]["edited"]:
        db.execute(
            "UPDATE scrum_reports SET window_start=?, window_end=?, generated_text=?, "
            "evidence=?, updated_at=? WHERE report_date=?",
            (win["start"].isoformat(), win["end"].isoformat(), summary_md, evidence, ts,
             report_date))
        report_id = existing[0]["id"]
        summary_text = existing[0]["summary_text"]
    elif existing:
        db.execute(
            "UPDATE scrum_reports SET window_start=?, window_end=?, summary_text=?, "
            "generated_text=?, evidence=?, updated_at=? WHERE report_date=?",
            (win["start"].isoformat(), win["end"].isoformat(), summary_md, summary_md,
             evidence, ts, report_date))
        report_id = existing[0]["id"]
        summary_text = summary_md
    else:
        report_id = db.execute(
            "INSERT INTO scrum_reports(report_date, window_start, window_end, summary_text, "
            "generated_text, evidence, edited, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,0,?,?)",
            (report_date, win["start"].isoformat(), win["end"].isoformat(), summary_md,
             summary_md, evidence, ts, ts))
        summary_text = summary_md

    db.audit("scrum_generated", {"date": report_date, "words": len(summary_md.split())},
             item_id=None)
    return {"report_id": report_id, "report_date": report_date, "summary_text": summary_text,
            "word_count": len(summary_text.split())}
