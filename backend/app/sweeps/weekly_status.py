"""Weekly status report.

Every Friday Chief gathers the week's evidence (merged PRs to dev in
arc-implementation-agents, ADO work items referenced, the daily notes and the
knowledge Chief synced), reads the previous week's Confluence page for format
and carry-forward, and synthesizes a status report in the same shape as the
existing pages under the parent. The result is created as an UNPUBLISHED
Confluence draft and surfaced on the board's Weekly Status tab.

Nothing goes public automatically: the user reviews the draft and hits Approve,
which publishes the Confluence page and posts the summary to the UseCases Tech
Lead chat (see api.approve_weekly_status).
"""

import asyncio
import html
import json
import logging
import re
from datetime import date, datetime, timedelta

from .. import confluence, db, vault
from ..agents import CHIEF, run_agent
from ..config import policy

log = logging.getLogger("chief.sweep.weekly_status")

REPO = (policy().get("weekly_status", {}) or {}).get("repo", "your-main-repo")
_GH_OWNER = (policy().get("github", {}) or {}).get("owner", "your-github-org")
REPO_URL = f"https://github.com/{_GH_OWNER}/{REPO}"
AB_RE = re.compile(r"\bAB#(\d+)", re.I)

WEEKLY_SCHEMA = {
    "type": "object",
    "properties": {
        "bottom_line": {"type": "string",
                        "description": "One punchy sentence: the single most important "
                                       "outcome of the week. No em dashes."},
        "what_shipped": {
            "type": "array",
            "description": "The concrete things delivered this week, most important first.",
            "items": {
                "type": "object",
                "properties": {
                    "emoji": {"type": "string", "description": "one leading emoji, e.g. ✅ 📈 🛠️ 📬 🧭"},
                    "headline": {"type": "string", "description": "short bold lead, a few words"},
                    "detail": {"type": "string", "description": "one clause of detail, plain text"},
                    "sub_bullets": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["emoji", "headline", "detail"],
            },
        },
        "by_the_numbers_extra": {
            "type": "array", "items": {"type": "string"},
            "description": "Optional extra metric lines beyond PRs/ADO (which are added in code).",
        },
        "demo": {"type": "array", "items": {"type": "string"},
                 "description": "Demos held or recorded this week, if any."},
        "watch_items": {
            "type": "array",
            "description": "Carry-forward risks/blockers/open items for next week.",
            "items": {
                "type": "object",
                "properties": {
                    "headline": {"type": "string"},
                    "detail": {"type": "string", "description": "optional context, may be empty"},
                },
                "required": ["headline"],
            },
        },
    },
    "required": ["bottom_line", "what_shipped", "watch_items"],
}

PROMPT = """You are writing the weekly status report for the user's program/team.
Audience: engineering leadership. Tone: crisp, factual, outcome-first. No em dashes.

This is for {week_label} ({date_range}). Base EVERYTHING on the evidence below;
do not invent shipped items or numbers. If evidence is thin, keep the report short
and honest rather than padding it. Hard metrics (PR counts, ADO counts) are added
by the system, so do not fabricate counts in what_shipped.

Carry forward any still-open items from last week's "Watch items" into this week's
watch_items unless the evidence shows they were resolved.

=== LAST WEEK'S PAGE (for format and carry-forward) ===
{prior}

=== THIS WEEK'S MERGED PRs (to dev in {repo}) ===
{prs}

=== ADO WORK ITEMS REFERENCED THIS WEEK ===
{ado}

=== DAILY NOTES, MEETINGS & KNOWLEDGE CHIEF GATHERED THIS WEEK ===
{context}
"""


def _esc(s: str) -> str:
    return html.escape(s or "", quote=True)


def _week_meta(today: date) -> dict:
    monday = today - timedelta(days=today.weekday())
    friday = monday + timedelta(days=4)
    iso = today.isocalendar()
    same_month = monday.month == friday.month
    rng = (f"{monday:%b} {monday.day}–{friday.day}, {friday.year}" if same_month
           else f"{monday:%b} {monday.day}–{friday:%b} {friday.day}, {friday.year}")
    return {
        "week_key": f"{iso.year}-W{iso.week:02d}",
        "week_label": f"Week {iso.week}",
        "next_label": f"Week {iso.week + 1}",
        "date_range": rng,
        "monday": monday,
        "friday": friday,
    }


async def _gh(*args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "gh", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    return proc.returncode, (out.decode() if proc.returncode == 0 else err.decode())


async def _gather_prs(meta: dict, cfg: dict) -> dict:
    """Merged PRs this week, split home (you) vs partner, plus ADO refs."""
    since = meta["monday"].isoformat()
    rc, out = await _gh(
        "pr", "list", "--repo", f"{_GH_OWNER}/{REPO}",
        "--base", cfg.get("base_branch", "dev"), "--state", "merged",
        "--search", f"merged:>={since}", "--limit", "200",
        "--json", "number,title,url,author,mergedAt,body")
    if rc != 0:
        log.warning("gh pr list failed: %s", out[:200])
        return {"prs": [], "total": 0, "realpage": 0, "partner": 0, "ado_refs": []}
    prs = json.loads(out)
    partner_logins = {l.lower() for l in cfg.get("partner_github_logins", [])}
    partner_label = cfg.get("partner_label", "partner")
    home_label = cfg.get("home_label", "Internal")
    ado_refs: set[str] = set()
    realpage = partner = 0
    brief = []
    for p in prs:
        login = ((p.get("author") or {}).get("login") or "").lower()
        is_partner = login in partner_logins
        partner += is_partner
        realpage += not is_partner
        ado_refs |= {m.group(1) for m in AB_RE.finditer(
            (p.get("title", "") + "\n" + (p.get("body") or "")))}
        brief.append(f"#{p['number']} [{partner_label if is_partner else home_label}] "
                     f"{p['title']}")
    return {"prs": prs, "total": len(prs), "realpage": realpage, "partner": partner,
            "partner_label": partner_label, "home_label": home_label, "ado_refs": sorted(ado_refs, key=int),
            "brief": brief}


def _gather_context(meta: dict) -> str:
    """Daily notes Mon-Fri + this week's Chieff activity log, as plain text."""
    parts: list[str] = []
    log_dir = vault.chieff_dir() / "Log"
    d = meta["monday"]
    while d <= meta["friday"]:
        note_p = vault.note_path(d)
        if note_p.exists():
            txt = note_p.read_text().strip()
            if txt:
                parts.append(f"--- Daily note {d.isoformat()} ---\n{txt[:4000]}")
        log_p = log_dir / f"{d.isoformat()}.md"
        if log_p.exists():
            txt = log_p.read_text().strip()
            if txt:
                parts.append(f"--- Chieff log {d.isoformat()} ---\n{txt[:4000]}")
        d += timedelta(days=1)
    return "\n\n".join(parts)[:20000] or "(no daily notes or traces found for this week)"


def _prior_page(cfg: dict) -> tuple[str, str]:
    """(markdownish text of the most recent sibling page, its title) for the agent."""
    try:
        children = confluence.child_pages(cfg["parent_page_id"], limit=25)
    except Exception as e:
        log.warning("could not list prior weekly pages: %s", e)
        return "", ""
    if not children:
        return "", ""
    # newest by version date; titles look like "Week NN ..."
    children.sort(key=lambda c: (c.get("version") or {}).get("when", ""), reverse=True)
    latest = children[0]
    try:
        page = confluence.get_page(latest["id"], expand="body.storage")
        storage = ((page.get("body") or {}).get("storage") or {}).get("value", "")
        from ..sweeps.knowledge import _html_to_md
        return _html_to_md(storage)[:8000], latest.get("title", "")
    except Exception as e:
        log.warning("could not read prior weekly page: %s", e)
        return "", latest.get("title", "")


# ---- rendering --------------------------------------------------------------

def _shipped_html(items: list[dict]) -> str:
    out = ["<ul>"]
    for it in items:
        emoji = _esc(it.get("emoji", "")).strip()
        head = _esc(it.get("headline", ""))
        detail = _esc(it.get("detail", ""))
        line = f"<li>{emoji} <strong>{head}</strong>"
        if detail:
            line += f" &mdash; {detail}"
        subs = [s for s in (it.get("sub_bullets") or []) if s.strip()]
        if subs:
            line += "<ul>" + "".join(f"<li>{_esc(s)}</li>" for s in subs) + "</ul>"
        line += "</li>"
        out.append(line)
    out.append("</ul>")
    return "".join(out)


def _watch_html(items: list[dict]) -> str:
    out = ["<ul>"]
    for it in items:
        head = _esc(it.get("headline", ""))
        detail = _esc(it.get("detail", "") or "")
        line = f"<li><strong>{head}</strong>"
        if detail:
            line += f" &mdash; {detail}"
        out.append(line + "</li>")
    out.append("</ul>")
    return "".join(out)


def _numbers_html(prs: dict, extra: list[str]) -> str:
    out = ["<ul>"]
    out.append(f"<li><strong>{prs['total']} PRs</strong> merged to <code>dev</code> &mdash; "
               f"{prs['realpage']} &rarr; {_esc(prs.get('home_label','Internal'))} &middot; {prs['partner']} &rarr; "
               f"{_esc(prs['partner_label'])}.</li>")
    if prs["ado_refs"]:
        out.append(f"<li><strong>{len(prs['ado_refs'])} ADO work items</strong> referenced.</li>")
    for line in extra or []:
        out.append(f"<li>{_esc(line)}</li>")
    out.append("</ul>")
    return "".join(out)


def _intro_html(meta: dict, prs: dict, leads: str, conf_url: str | None) -> str:
    ado_n = len(prs["ado_refs"])
    intro = (f'<strong>{meta["week_label"]} &middot; {_esc(meta["date_range"])}</strong>  |  '
             f'<strong>Leads:</strong> {_esc(leads)} '
             f'<em>{prs["total"]} PRs merged to <code>dev</code> in '
             f'<a href="{REPO_URL}"><code>{REPO}</code></a> &middot; '
             f'{ado_n} ADO work items referenced.</em>')
    if conf_url:
        intro += (f' &nbsp;❗<a href="{conf_url}"><em>Extended Weekly status in Confluence'
                  f'</em></a>❗')
    return intro


def render_confluence(meta: dict, prs: dict, result: dict, leads: str) -> str:
    _rt = policy().get('weekly_status', {}).get('report_title', 'Weekly Status')
    p = [f"<h1>{_esc(_rt)}</h1>",
         f"<p>{_intro_html(meta, prs, leads, None)}</p>",
         "<hr />",
         "<h2>Executive Summary</h2>",
         f"<blockquote><p><strong>Bottom line:</strong> {_esc(result['bottom_line'])}</p></blockquote>",
         "<p><strong>What shipped</strong></p>",
         _shipped_html(result.get("what_shipped", []))]
    if result.get("demo"):
        p.append("<p><strong>Demo</strong></p><ul>"
                 + "".join(f"<li>{_esc(x)}</li>" for x in result["demo"]) + "</ul>")
    p.append("<p><strong>By the numbers</strong></p>")
    p.append(_numbers_html(prs, result.get("by_the_numbers_extra", [])))
    p.append(f"<h2>Watch items &rarr; {meta['next_label']}</h2>")
    p.append(_watch_html(result.get("watch_items", [])))
    return "".join(p)


def render_teams(meta: dict, prs: dict, result: dict, leads: str, *,
                 conf_url: str, mention: str | None) -> str:
    p = []
    if mention:
        p.append(f"<p>@{_esc(mention)},</p>")
    _rt = policy().get('weekly_status', {}).get('report_title', 'Weekly Status')
    p.append(f"<p><strong>{_esc(_rt)}</strong></p>")
    p.append(f"<p>{_intro_html(meta, prs, leads, conf_url)}</p>")
    p.append("<p><strong>Executive Summary</strong></p>")
    p.append(f"<blockquote><p><strong>Bottom line:</strong> {_esc(result['bottom_line'])}</p></blockquote>")
    p.append("<p><strong>What shipped</strong></p>")
    p.append(_shipped_html(result.get("what_shipped", [])))
    if result.get("demo"):
        p.append("<p><strong>Demo</strong></p><ul>"
                 + "".join(f"<li>{_esc(x)}</li>" for x in result["demo"]) + "</ul>")
    p.append("<p><strong>By the numbers</strong></p>")
    p.append(_numbers_html(prs, result.get("by_the_numbers_extra", [])))
    p.append(f"<p><strong>Watch items &rarr; {meta['next_label']}</strong></p>")
    p.append(_watch_html(result.get("watch_items", [])))
    return "".join(p)


# ---- entry point ------------------------------------------------------------

async def generate(target: date | None = None) -> dict:
    """Build (or rebuild) this week's draft report and create a Confluence draft.
    Idempotent per ISO week: regenerating replaces the prior draft for the week."""
    cfg = policy().get("weekly_status", {})
    if not cfg.get("parent_page_id") or not cfg.get("space"):
        return {"skipped": "weekly_status.parent_page_id/space not set in policy.yaml"}
    today = target or date.today()
    meta = _week_meta(today)
    leads = cfg.get("leads") or policy().get("me", {}).get("name", "")

    prs = await _gather_prs(meta, cfg)
    prior_md, prior_title = await asyncio.to_thread(_prior_page, cfg)
    context = await asyncio.to_thread(_gather_context, meta)

    ado_txt = (", ".join(f"AB#{r}" for r in prs["ado_refs"])
               if prs["ado_refs"] else "(none referenced in PR titles/bodies)")
    prompt = PROMPT.format(
        week_label=meta["week_label"], date_range=meta["date_range"], repo=REPO,
        prior=prior_md or "(no prior page found)",
        prs="\n".join(prs.get("brief", [])) or "(no PRs merged to dev this week)",
        ado=ado_txt, context=context)
    result = await asyncio.wait_for(
        run_agent(CHIEF, prompt, schema=WEEKLY_SCHEMA, with_tools=False, max_turns=8,
                  model=cfg.get("model", "claude-sonnet-4-6"), label="weekly_status"),
        timeout=int(cfg.get("timeout_seconds", 300)))
    if not isinstance(result, dict):
        raise RuntimeError("weekly status synthesis produced no structured result")

    storage_html = render_confluence(meta, prs, result, leads)
    title = f"{meta['week_label']} — {cfg.get('report_title', 'Weekly Status')} ({meta['date_range']})"

    # Create the unpublished Confluence draft. If Confluence write is not
    # available (e.g. the API token is read-only/anonymous), still persist the
    # report so it is reviewable on the board; publishing happens at approve.
    existing = db.query("SELECT * FROM weekly_reports WHERE week_key=?", (meta["week_key"],))
    page = {"id": None, "page_url": None, "edit_url": None}
    conf_error = None
    try:
        page = confluence.create_page(cfg["space"], cfg["parent_page_id"], title,
                                      storage_html, draft=True)
    except Exception as e:
        conf_error = str(e)[:200]
        log.warning("Confluence draft create failed (saving report anyway): %s", conf_error)
    teams_html = render_teams(meta, prs, result, leads, conf_url=page["page_url"],
                              mention=cfg.get("mention"))
    metrics = {"prs": prs["total"], "realpage": prs["realpage"],
               "partner": prs["partner"], "partner_label": prs["partner_label"],
               "ado_refs": prs["ado_refs"]}
    ts = db.now()
    if existing:
        db.execute(
            "UPDATE weekly_reports SET week_label=?, date_range=?, title=?, "
            "confluence_page_id=?, confluence_url=?, edit_url=?, storage_html=?, "
            "teams_html=?, metrics=?, status='draft', updated_at=? WHERE week_key=?",
            (meta["week_label"], meta["date_range"], title, page["id"], page["page_url"],
             page["edit_url"], storage_html, teams_html, json.dumps(metrics), ts,
             meta["week_key"]))
        report_id = existing[0]["id"]
    else:
        report_id = db.execute(
            "INSERT INTO weekly_reports(week_key, week_label, date_range, title, "
            "confluence_page_id, confluence_url, edit_url, storage_html, teams_html, "
            "metrics, status, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,'draft',?,?)",
            (meta["week_key"], meta["week_label"], meta["date_range"], title, page["id"],
             page["page_url"], page["edit_url"], storage_html, teams_html,
             json.dumps(metrics), ts, ts))

    db.audit("weekly_status_drafted",
             {"week": meta["week_key"], "title": title, "page_id": page["id"],
              "metrics": metrics, "confluence_error": conf_error}, item_id=None)
    vault.chieff_trace("Confluence", f"weekly status draft ready: {title} "
                                     f"({prs['total']} PRs, {len(prs['ado_refs'])} ADO refs)")
    return {"report_id": report_id, "week": meta["week_key"], "title": title,
            "draft_url": page["edit_url"], "metrics": metrics, "status": "draft",
            "confluence_error": conf_error}
