"""Agent swarm: read the ADO board, fan out ephemeral workers, keep receipts.

Workers are cattle, receipts are pets. Every review, investigation and code
change runs in a throwaway worker — fresh context, fresh git worktree — so
nothing drifts, nothing waits on anything else, and a bad run costs nothing;
you just spawn another. The only things that persist are the receipts:
verdicts in swarm_runs/swarm_jobs, evidence comments on tickets and PRs, the
pinned matrix on the board, and the runbooks that define the checks.

One orchestrator run pulls qualifying work items (policy.yaml swarm:), matches
each against the runbooks in runbooks/, and fans out ONE run_agent() call per
(item, runbook) pair. A fresh context per call is what makes workers ephemeral
and disposable; the durable part is the receipts: structured verdicts in
swarm_runs/swarm_jobs and the audit log, plus an evidence comment on the ADO
item and a Teams run summary, both routed through actions.py so the kill
switch and dry run always apply. Live progress streams to the board's Swarm
tab via SSE (swarm_run_update / swarm_job_update).

Read workers are tool-free (run_agent enforces it): evidence is pre-fetched
here and embedded in the prompt, so a worker can never touch anything outside
its context. CODE workers live in worktrees.run_worker(): write access only
inside a throwaway git worktree, and nothing is pushed or PR'd until the user
approves on the Worktrees tab (actions.swarm_pr_open, kill switch + dry run).
Never loosen run_agent's read-only tool restrictions; the code path is a
separate harness on purpose.
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass

import yaml

from . import ado, db, events, gates
from .agents import DEFAULT_TIMEOUT_S, AgentDef, run_agent
from .config import REPO_ROOT, policy

log = logging.getLogger("chief.swarm")

RUNBOOKS_DIR = REPO_ROOT / "runbooks"

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["pass", "fail", "blocked"],
                   "description": "pass = healthy per the runbook; fail = something "
                                  "concrete is wrong; blocked = cannot judge from the data"},
        "headline": {"type": "string", "description": "verdict in one short line, under 15 words"},
        "evidence": {"type": "array", "items": {"type": "string"},
                     "description": "specific signals checked: quote ids, dates, states"},
        "suggested_next_action": {"type": "string",
                                  "description": "single most useful next step for the item owner"},
        "clarifying_questions": {"type": "array", "items": {"type": "string"},
                                 "description": "questions a human must answer before this "
                                                "target is actionable; empty when clear"},
        "workable": {"type": "boolean",
                     "description": "ADO tickets only: true when an automated coding worker "
                                    "could plausibly implement the fix NOW (clear, small, "
                                    "self-contained). false for PRs or when unsure."},
    },
    "required": ["status", "headline", "evidence", "suggested_next_action", "workable"],
}

_FINDING = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "one-line finding"},
        "where": {"type": "string", "description": "file/function it lives in"},
        "why": {"type": "string", "description": "why it matters, one sentence"},
    },
    "required": ["title", "where", "why"],
}

# pr-review runbook: only findings that matter — critical blocks merge,
# tech_debt feeds the tech-debt swarm as future worktree work.
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["pass", "fail", "blocked"]},
        "headline": {"type": "string"},
        "critical_issues": {"type": "array", "items": _FINDING},
        "tech_debt": {"type": "array", "items": _FINDING},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "suggested_next_action": {"type": "string"},
    },
    "required": ["status", "headline", "critical_issues", "tech_debt",
                 "evidence", "suggested_next_action"],
}

# ticket-code-plan runbook: is this ticket safe for an automated coding worker,
# and if so, exactly what should it do.
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["pass", "fail", "blocked"],
                   "description": "pass = codeable now"},
        "headline": {"type": "string"},
        "codeable": {"type": "boolean"},
        "change_plan": {"type": "string",
                        "description": "instructions for the coding worker; empty if not codeable"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "suggested_next_action": {"type": "string"},
        "clarifying_questions": {"type": "array", "items": {"type": "string"},
                                 "description": "what a human must answer before this ticket "
                                                "is codeable; MUST be empty when codeable"},
    },
    "required": ["status", "headline", "codeable", "change_plan",
                 "evidence", "suggested_next_action", "clarifying_questions"],
}

COMMENT_GATE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["post", "skip"],
                   "description": "skip when the proposed comment adds no material "
                                  "new information vs what is already on the ticket"},
        "comment_html": {"type": "string",
                         "description": "when posting: the comment REWRITTEN to only "
                                        "the new/changed information, same HTML "
                                        "conventions; empty when skipping"},
        "reason": {"type": "string", "description": "one line: why post or skip"},
    },
    "required": ["action", "comment_html", "reason"],
}

GATE = AgentDef(
    name="swarm_comment_gate",
    system_prompt=(
        "You are the anti-spam gate for swarm comments on Azure DevOps tickets. "
        "You receive the ticket's existing comments (newest first, 🤖 marks earlier "
        "swarm comments) and one proposed new swarm comment. Rules: if the proposal "
        "adds no material new information beyond the existing comments, action=skip. "
        "If it partially overlaps, action=post but REWRITE comment_html to only the "
        "new or changed facts, referencing earlier comments instead of repeating them "
        "(keep the 🤖 <b>Run #n</b> header and HTML list formatting). Never repeat "
        "questions that were already asked and are still unanswered. If the proposal "
        "is entirely new information, action=post with the proposal unchanged."
    ),
)

SCHEMAS = {"verdict": VERDICT_SCHEMA, "review": REVIEW_SCHEMA, "plan": PLAN_SCHEMA}

# Which runbooks each profile fans out (policy swarm.profiles overrides).
# The board tabs map to these: ADO Swarm -> ado_scan | ticket, PR Swarm ->
# pr_scan | pr_review, Debt Swarm -> tech_debt (no read phase: it goes straight
# to coder worktrees fed by earlier pr_review findings). 'board' is the legacy
# everything scan kept for the API.
PROFILE_RUNBOOKS = {
    "board": ["ticket-investigate", "pr-status", "smoke-test", "pr-health"],
    "ado_scan": ["ticket-investigate", "pr-status", "smoke-test"],
    "ticket": ["ticket-investigate", "ticket-code-plan"],
    # thread pipeline: same runbooks as ticket, but dispatched one ticket per
    # thread, walked end-to-end (see _thread_flow)
    "thread": ["ticket-investigate", "ticket-code-plan"],
    "pr_scan": ["pr-health"],
    "pr_review": ["pr-review"],
    "tech_debt": [],
}

WORKER = AgentDef(
    name="swarm_worker",
    system_prompt=(
        "You are a swarm worker: one ephemeral, read-only quality check on a single "
        "work target (an Azure DevOps work item or a GitHub pull request) for the "
        "owner's chief-of-staff system. You receive a runbook and the target's data "
        "and return ONLY the structured verdict. Judge strictly from the provided data "
        "and never invent evidence. Keep the headline under 15 words; keep each "
        "evidence entry short and specific (quote ids, dates, states). No em dashes."
    ),
)

REVIEWER = AgentDef(
    name="swarm_reviewer",
    system_prompt=(
        "You are the swarm reviewer. You read every worker verdict from one swarm run "
        "over the owner's ADO board and open pull requests, and write the run summary: 2 to "
        "4 sentences on what matters most (broken targets first, then themes), followed "
        "by a compact markdown table with columns target | runbook | verdict | headline. "
        "Be factual, cite item/PR ids, and never soften a fail. No em dashes."
    ),
)


@dataclass
class Runbook:
    name: str
    title: str
    body: str
    targets: list[str]   # target kinds it applies to: ado | pr (default ado)
    types: list[str]     # ADO: lowercased work-item types it applies to; empty = any
    buckets: list[str]   # ADO: lowercased state buckets it applies to; empty = any
    timeout_s: float
    schema: str = "verdict"     # verdict | review | plan (see SCHEMAS)
    include_diff: bool = False  # PR runbooks: embed the full gh pr diff


_FM_RE = re.compile(r"^---\n(.*?)\n---\n", re.S)


def load_runbooks(cfg: dict) -> list[Runbook]:
    """Parse runbooks/*.md: YAML frontmatter (title, applies) + prompt body.
    Per-runbook timeouts come from policy swarm.timeouts, default = harness's 5 min."""
    timeouts = cfg.get("timeouts") or {}
    out = []
    for path in sorted(RUNBOOKS_DIR.glob("*.md")):
        if path.name.startswith("_"):
            continue  # docs/templates, not runbooks
        raw = path.read_text()
        meta, body = {}, raw
        m = _FM_RE.match(raw)
        if m:
            try:
                meta = yaml.safe_load(m.group(1)) or {}
            except Exception:
                log.warning("runbook %s has invalid frontmatter; using defaults", path.name)
            body = raw[m.end():]
        applies = meta.get("applies") or {}
        out.append(Runbook(
            name=path.stem,
            title=str(meta.get("title", path.stem)),
            body=body.strip(),
            targets=[str(t).lower() for t in applies.get("targets") or ["ado"]],
            types=[str(t).lower() for t in applies.get("types") or []],
            buckets=[str(b).lower() for b in applies.get("buckets") or []],
            timeout_s=float(timeouts.get(path.stem, DEFAULT_TIMEOUT_S)),
            schema=str(meta.get("schema", "verdict")),
            include_diff=bool(meta.get("include_diff", False)),
        ))
    return out


def profile_runbooks(profile: str, cfg: dict) -> list[Runbook]:
    """The profile's runbook set: policy swarm.profiles.<name>.runbooks wins,
    then the built-in defaults; unknown names are ignored."""
    names = ((cfg.get("profiles") or {}).get(profile, {}) or {}).get(
        "runbooks", PROFILE_RUNBOOKS.get(profile))
    books = load_runbooks(cfg)
    if names is None:
        return books
    by_name = {b.name: b for b in books}
    return [by_name[n] for n in names if n in by_name]


def _matches(rb: Runbook, t: dict) -> bool:
    if (t.get("kind") or "ado") not in rb.targets:
        return False
    if t.get("kind") == "ado":
        if rb.types and (t.get("type") or "").lower() not in rb.types:
            return False
        if rb.buckets and (t.get("bucket") or "").lower() not in rb.buckets:
            return False
    return True


def _ado_candidates(cfg: dict, item_ids: list[int] | None) -> list[dict]:
    """Qualifying work items: an explicit id list, or the user's board filtered by
    the projects/states in policy swarm: (my_items is already priority-sorted)."""
    if item_ids:
        items = ado.items_by_ids([int(i) for i in item_ids])
    else:
        data = ado.my_items()
        if data.get("error"):
            raise RuntimeError(data["error"])
        projects = {p.lower() for p in cfg.get("projects") or []}
        states = {s.lower() for s in cfg.get("states") or []}
        items = [it for it in data["items"]
                 if (not projects or it["project"].lower() in projects)
                 and (not states or it["state"].lower() in states)]
        items = items[: int(cfg.get("max_items", 5))]
    for it in items:
        it["kind"] = "ado"
    return items


# ---- GitHub PR targets ---------------------------------------------------------

async def _gh_json(*args: str):
    proc = await asyncio.create_subprocess_exec(
        "gh", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0:
        log.warning("gh %s failed: %s", " ".join(args[:3]), err.decode()[:200])
        return None
    return json.loads(out.decode())


async def _pr_candidates(cfg: dict) -> list[dict]:
    """Open PRs from the user's perspective: awaiting their review, plus their own."""
    pcfg = cfg.get("pr") or {}
    if not pcfg.get("enabled", True):
        return []
    limit = int(pcfg.get("max_items", 4))
    fields = "number,title,url,repository,updatedAt,isDraft"
    awaiting, mine = await asyncio.gather(
        _gh_json("search", "prs", "--review-requested=@me", "--state=open",
                 "--limit", str(limit), "--json", fields),
        _gh_json("search", "prs", "--author=@me", "--state=open",
                 "--limit", str(limit), "--json", fields))
    seen: set[str] = set()
    out: list[dict] = []
    for role, prs in (("awaiting your review", awaiting), ("your PR", mine)):
        for pr in prs or []:
            repo = (pr.get("repository") or {}).get("nameWithOwner") or ""
            key = f"{repo}#{pr.get('number')}"
            if not repo or key in seen:
                continue
            seen.add(key)
            out.append({
                "kind": "pr", "id": pr.get("number"),
                "title": pr.get("title", ""), "type": "PR",
                "state": "draft" if pr.get("isDraft") else role,
                "bucket": role, "project": repo, "url": pr.get("url", ""),
            })
    return out[:limit]


def _pr_org() -> str:
    return (policy().get("swarm", {}).get("pr", {}).get("org")
            or policy().get("github", {}).get("owner", "your-github-org"))


def _shape_pr(pr: dict, repo: str) -> dict:
    m = re.search(r"AB#(\d+)", pr.get("title") or "")
    return {
        "repo": repo, "number": pr.get("number"),
        "title": pr.get("title", ""),
        "author": (pr.get("author") or {}).get("login", ""),
        "updated": str(pr.get("updatedAt", ""))[:10],
        "draft": bool(pr.get("isDraft")),
        "url": pr.get("url", ""),
        "ab": m.group(1) if m else None,
    }


async def org_repos() -> list[str]:
    """Org repos matching the swarm's prefixes, for the PR picker's Repo filter."""
    cfg = policy().get("swarm", {}).get("pr", {})
    prefixes = cfg.get("repo_prefixes", ["arc-"])
    repos = await _gh_json("repo", "list", _pr_org(), "--limit", "200",
                           "--json", "name", "--no-archived") or []
    names = sorted(r["name"] for r in repos
                   if any(r.get("name", "").startswith(p) for p in prefixes))
    return names


async def pr_candidates_list(repo: str | None = None) -> list[dict]:
    """PR Swarm picker candidates. Default: open PRs involving the user. With a
    repo selected: ALL open PRs in that repo (anyone's), so whole repos can be
    swarmed. Linked ADO ticket parsed from AB# in the title."""
    if repo:
        full = repo if "/" in repo else f"{_pr_org()}/{repo}"
        prs = await _gh_json("pr", "list", "--repo", full, "--state", "open",
                             "--limit", "50", "--json",
                             "number,title,url,updatedAt,isDraft,author") or []
        return [_shape_pr(pr, full) for pr in prs]
    fields = "number,title,url,repository,updatedAt,isDraft,author"
    prs = await _gh_json("search", "prs", "--involves=@me", "--state=open",
                         "--limit", "50", "--json", fields) or []
    out = []
    for pr in prs:
        r = (pr.get("repository") or {}).get("nameWithOwner") or ""
        if r:
            out.append(_shape_pr(pr, r))
    return out


async def _pr_targets_from_refs(refs: list[dict]) -> list[dict]:
    """Explicitly-selected PRs from the picker -> swarm targets."""
    async def one(r):
        d = await _gh_json("pr", "view", str(r["number"]), "--repo", r["repo"],
                           "--json", "number,title,url,isDraft")
        if not d:
            return None
        return {"kind": "pr", "id": d["number"], "title": d.get("title", ""),
                "type": "PR", "state": "draft" if d.get("isDraft") else "selected",
                "bucket": "selected", "project": r["repo"], "url": d.get("url", "")}
    res = await asyncio.gather(*[one(r) for r in refs])
    return [t for t in res if t]


async def _pr_details(repo: str, number: int) -> dict:
    """Everything a PR worker may use as evidence, trimmed to prompt size."""
    data = await _gh_json(
        "pr", "view", str(number), "--repo", repo, "--json",
        "number,title,body,state,isDraft,author,createdAt,updatedAt,additions,"
        "deletions,changedFiles,files,reviews,reviewRequests,latestReviews,"
        "statusCheckRollup,mergeable,labels,comments,baseRefName,headRefName,url")
    if not data:
        raise RuntimeError(f"gh pr view failed for {repo}#{number}")
    data["repo"] = repo
    data["author"] = (data.get("author") or {}).get("login")
    data["body"] = (data.get("body") or "")[:4000]
    data["labels"] = [l.get("name") for l in (data.get("labels") or [])]
    data["files"] = [{"path": f.get("path"), "additions": f.get("additions"),
                      "deletions": f.get("deletions")}
                     for f in (data.get("files") or [])[:60]]
    data["comments"] = [{"author": (c.get("author") or {}).get("login"),
                         "at": str(c.get("createdAt", ""))[:16],
                         "body": (c.get("body") or "")[:600]}
                        for c in (data.get("comments") or [])[-12:]]
    for k in ("reviews", "latestReviews"):
        data[k] = [{"author": (r.get("author") or {}).get("login"),
                    "state": r.get("state"),
                    "at": str(r.get("submittedAt", ""))[:16],
                    "body": (r.get("body") or "")[:400]}
                   for r in (data.get(k) or [])[-12:]]
    data["reviewRequests"] = [(r.get("login") or r.get("name") or "")
                              for r in (data.get("reviewRequests") or [])]
    data["statusCheckRollup"] = [
        {"name": c.get("name") or c.get("context"),
         "status": c.get("status") or c.get("state"),
         "conclusion": c.get("conclusion")}
        for c in (data.get("statusCheckRollup") or [])[:40]]
    return data


# ---- per-job cost attribution --------------------------------------------------
# run_agent meters every call into cos_cost under its label; a job claims the
# newest unclaimed row its label wrote after the job started. Claim bookkeeping
# is synchronous within the event loop, so no lock is needed.

_claimed_cost_ids: set[int] = set()


def _max_cost_id() -> int:
    return db.query("SELECT MAX(id) AS m FROM cos_cost")[0]["m"] or 0


def _claim_cost(label: str, after_id: int) -> float | None:
    rows = db.query("SELECT id, cost_usd FROM cos_cost WHERE label=? AND id>? "
                    "ORDER BY id DESC LIMIT 8", (label, after_id))
    for r in rows:
        if r["id"] not in _claimed_cost_ids:
            _claimed_cost_ids.add(r["id"])
            return r["cost_usd"]
    return None


# ---- live events ----------------------------------------------------------------

def _job_dict(job_id: int) -> dict | None:
    rows = db.query("SELECT * FROM swarm_jobs WHERE id=?", (job_id,))
    if not rows:
        return None
    j = rows[0]
    j["verdict"] = json.loads(j["verdict"]) if j["verdict"] else None
    return j


def _emit_job(job_id: int) -> None:
    j = _job_dict(job_id)
    if j:
        events.broadcast("swarm_job_update", {"run_id": j["run_id"], "job": j})


def _emit_run(run_id: int) -> None:
    rows = db.query("SELECT * FROM swarm_runs WHERE id=?", (run_id,))
    if rows:
        r = rows[0]
        r["totals"] = json.loads(r["totals"]) if r["totals"] else None
        events.broadcast("swarm_run_update", {"run": r})


def _emit_phase(run_id: int, job_id: int, phase: str, note: str = "",
                attempt: int = 0, ref: str = "", runbook: str = "") -> None:
    """Live narration for the strategy board: which zone a unit is in
    (spawn -> evidence -> reasoning -> verdict, back to evidence on retry)
    and what it is doing right now. Also persisted on the job row so a browser
    that joins mid-run (or the polling fallback) places units truthfully."""
    db.execute("UPDATE swarm_jobs SET phase=?, phase_note=? WHERE id=?",
               (phase, note[:160], job_id))
    events.broadcast("swarm_job_phase", {
        "run_id": run_id, "job_id": job_id, "phase": phase,
        "note": note[:160], "attempt": attempt, "ref": ref, "runbook": runbook})


# ---- the orchestrator -------------------------------------------------------------

async def run_swarm(trigger: str = "manual", item_ids: list[int] | None = None,
                    run_id: int | None = None, profile: str = "board",
                    pr_refs: list[dict] | None = None,
                    concurrency: int | None = None) -> dict:
    """One full swarm pass. Creates the run row unless start_run() already did.

    Profiles: board (read-only hygiene over ADO items + PRs), ticket
    (investigate + code-plan, then coder worktrees for codeable tickets),
    pr_review (diff review of PRs: critical issues + tech debt), tech_debt
    (coder worktrees fixing tech-debt findings from earlier pr_review runs)."""
    cfg = policy().get("swarm", {})
    _reap_orphans()
    try:
        from . import worktrees as _wt
        await _wt.reap_expired()
        shipped = await _wt.ship_ready()
        for sh in shipped:
            if sh.get("url"):
                log.info("swarm self-shipped %s -> %s", sh["branch"], sh["url"])
        for c in await _wt.ci_watch():
            log.info("swarm CI: %s -> %s %s", c["branch"], c["ci"], c.get("fix", ""))
        for s in await _wt.pr_sync():
            if s.get("ticket") or s.get("ci"):
                log.info("swarm sync: %s -> %s %s", s["branch"], s["pr_state"],
                         s.get("ticket") or s.get("ci"))
        for u in await _wt.uat_watch():
            log.info("swarm uat: %s -> %s", u["branch"], u.get("uat") or u.get("smoke"))
        for a in await _wt.announce_green():
            log.info("swarm announce: %s -> %s", a["branch"], a["status"])
    except Exception:
        log.exception("worktree housekeeping failed (harmless)")
    if not cfg.get("enabled", True):
        if run_id:
            _fail_run(run_id, "swarm.enabled is false in policy.yaml")
        return {"skipped": "swarm.enabled is false in policy.yaml"}
    active = db.query(
        "SELECT id FROM swarm_runs WHERE status='running' AND id<>? AND started_at>? LIMIT 1",
        (run_id or 0, db.cutoff(minutes=30)))
    if active:
        if run_id:
            _fail_run(run_id, f"run #{active[0]['id']} is still active")
        return {"skipped": f"swarm run #{active[0]['id']} is still active"}
    if run_id is None:
        run_id = db.execute(
            "INSERT INTO swarm_runs(trigger, profile, started_at) VALUES(?,?,?)",
            (trigger, profile, db.now()))
    else:
        db.execute("UPDATE swarm_runs SET profile=? WHERE id=?", (profile, run_id))
    t0 = time.monotonic()
    _emit_run(run_id)
    try:
        runbooks = profile_runbooks(profile, cfg)
        targets: list[dict] = []
        if any("ado" in rb.targets for rb in runbooks):
            targets += await asyncio.to_thread(_ado_candidates, cfg, item_ids)
        if pr_refs:  # explicit picker selection wins over the auto sweep
            targets += await _pr_targets_from_refs(pr_refs)
        elif not item_ids and any("pr" in rb.targets for rb in runbooks):
            targets += await _pr_candidates(cfg)
        thread_wts: list[dict] | None = None
        if profile == "thread":
            # thread architecture: no upfront (target x runbook) fan-out — the
            # dispatcher queues tickets and threads create jobs as they walk them
            thread_wts = await _thread_flow(run_id, targets, cfg, concurrency)
        else:
            pairs = [(t, rb) for t in targets for rb in runbooks if _matches(rb, t)]
            model = cfg.get("worker_model") or WORKER.model
            with db.transaction():
                job_ids = [db.execute(
                    "INSERT INTO swarm_jobs(run_id,kind,item_id,item_title,item_type,"
                    "item_state,item_project,item_url,runbook,model) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (run_id, t["kind"], t["id"], t["title"], t["type"], t["state"],
                     t["project"], t["url"], rb.name, model)) for t, rb in pairs]
            _emit_run(run_id)  # jobs are queued; the panel refetches and shows the grid
            log.info("swarm run %s: %d targets -> %d workers (cap %s)",
                     run_id, len(targets), len(pairs),
                     concurrency or cfg.get("max_concurrent", 8))

            cap = int(concurrency or cfg.get("max_concurrent", 8))
            sem = asyncio.Semaphore(cap)
            await asyncio.gather(*[
                _run_job(jid, t, rb, sem, cfg, run_id)
                for jid, (t, rb) in zip(job_ids, pairs)])

        jobs = db.query("SELECT * FROM swarm_jobs WHERE run_id=? ORDER BY id", (run_id,))
        totals = {
            "jobs": len(jobs),
            "items": len({(j["kind"], j["item_id"]) for j in jobs}),
            "pass": sum(1 for j in jobs if j["status"] == "pass"),
            "fail": sum(1 for j in jobs if j["status"] == "fail"),
            "blocked": sum(1 for j in jobs if j["status"] == "blocked"),
            "error": sum(1 for j in jobs if j["status"] == "error"),
            "cost_usd": round(sum(j["cost_usd"] or 0.0 for j in jobs), 4),
        }
        wts = thread_wts if thread_wts is not None \
            else await _code_phase(run_id, profile, jobs, cfg)
        if wts:
            totals["worktrees"] = wts
        summary = await _review(jobs, cfg)
        totals["receipts"] = await _receipts(run_id, jobs, totals, summary, cfg)
        db.execute(
            "UPDATE swarm_runs SET finished_at=?, duration_ms=?, status='done', "
            "totals=?, summary=? WHERE id=?",
            (db.now(), int((time.monotonic() - t0) * 1000),
             json.dumps(totals, ensure_ascii=False), summary, run_id))
        db.audit("swarm_run", {"run_id": run_id, "trigger": trigger,
                               "profile": profile, **totals}, actor="swarm")
        _emit_run(run_id)
        _run_ctl.pop(run_id, None)
        # pipeline autonomy: a scan that found workable tickets flows straight
        # into the coding stage, no button press needed
        if profile in ("ado_scan", "board") and cfg.get("auto_code", True):
            workable = sorted({j["item_id"] for j in jobs
                               if (j["kind"] or "ado") == "ado" and j["verdict"]
                               and json.loads(j["verdict"]).get("workable")})
            if workable:
                log.info("swarm run %s: auto-starting thread pipeline on workable %s",
                         run_id, workable)
                start_run(trigger="auto", item_ids=workable, profile="thread")
        return {"run_id": run_id, "profile": profile, **totals}
    except asyncio.CancelledError:
        # HITL stop from the board: close out cleanly, keep the receipts so far.
        # Deliberately absorbed (not re-raised) so health.tracked records the
        # run as finished instead of leaving a forever-'running' job_runs row.
        db.execute("UPDATE swarm_jobs SET status='error', error='stopped by the user' "
                   "WHERE run_id=? AND status IN ('queued','running')", (run_id,))
        db.execute(
            "UPDATE swarm_runs SET finished_at=?, duration_ms=?, status='stopped', "
            "error='stopped by the user' WHERE id=?",
            (db.now(), int((time.monotonic() - t0) * 1000), run_id))
        db.audit("swarm_stopped", {"run_id": run_id}, actor="user")
        _emit_run(run_id)
        return {"run_id": run_id, "stopped": True}
    except Exception as e:
        db.execute(
            "UPDATE swarm_runs SET finished_at=?, duration_ms=?, status='error', error=? "
            "WHERE id=?",
            (db.now(), int((time.monotonic() - t0) * 1000), str(e)[:500], run_id))
        _emit_run(run_id)
        raise


async def _run_job(job_id: int, target: dict, rb: Runbook, sem: asyncio.Semaphore,
                   cfg: dict, run_id: int) -> None:
    """One ephemeral worker: fetch evidence, one run_agent call, persist the verdict.
    Never raises — a broken worker is a receipt (status=error), not a broken run."""
    async with sem:
        tkey = f"{target['kind']}:{target['id']}"
        if not await _wait_clearance(run_id, tkey):
            db.execute("UPDATE swarm_jobs SET status='error', error='stopped by the user', "
                       "finished_at=? WHERE id=?", (db.now(), job_id))
            _emit_job(job_id)
            return
        db.execute("UPDATE swarm_jobs SET status='running', started_at=? WHERE id=?",
                   (db.now(), job_id))
        _emit_job(job_id)
        t0 = time.monotonic()
        label = f"swarm:{rb.name}"
        cost_after = _max_cost_id()
        status, verdict, error = "error", None, None
        # self-healing: a failed worker retries with a fresh context before it
        # becomes an error receipt (evidence fetch + agent call both covered);
        # the loop count is the Active->Blocked gate on the board
        attempts = 1 + gates.gate_value("retries")
        ref = f"{target['project'].split('/')[-1]}#{target['id']}" \
            if target["kind"] == "pr" else f"#{target['id']}"
        for attempt in range(attempts):
            try:
                _emit_phase(run_id, job_id, "evidence",
                            f"gathering evidence for {ref}", attempt,
                            ref=ref, runbook=rb.name)
                if target["kind"] == "pr":
                    detail = await _pr_details(target["project"], target["id"])
                    what = "Pull request data"
                    gathered = (f"{detail.get('changedFiles', 0)} files · "
                                f"+{detail.get('additions', 0)}/-{detail.get('deletions', 0)} · "
                                f"{len(detail.get('reviews') or [])} reviews")
                else:
                    detail = await asyncio.to_thread(ado.item_details, target["id"])
                    what = "Work item data"
                    # answers can arrive in the UAT team chat, not only on the
                    # ticket — inject them as evidence when questions are pending
                    if db.query("SELECT 1 FROM audit_log WHERE action='swarm_clarify' "
                                "AND json_extract(detail,'$.ado_item')=? "
                                "AND json_extract(detail,'$.dry_run') IS NULL LIMIT 1",
                                (target["id"],)):
                        chat_answers = await _uat_chat_mentions(target["id"])
                        if chat_answers:
                            detail["teams_chat_answers"] = chat_answers
                    gathered = (f"{len(detail.get('comments') or [])} comments · "
                                f"{len(detail.get('relations') or [])} links · "
                                f"state {detail.get('state', '?')}")
                prompt = (f"# Runbook: {rb.title}\n\n{rb.body}\n\n"
                          f"# {what} (the ONLY evidence you may use)\n\n"
                          f"{json.dumps(detail, ensure_ascii=False, indent=1)[:24000]}")
                if rb.include_diff and target["kind"] == "pr":
                    diff = await _pr_diff(target["project"], target["id"],
                                          int(cfg.get("max_diff_chars", 50000)))
                    prompt += f"\n\n# Full diff\n\n{diff}"
                    gathered += f" · {len(diff) // 1000}k diff"
                _emit_phase(run_id, job_id, "reasoning",
                            f"{gathered} — applying {rb.title}", attempt,
                            ref=ref, runbook=rb.name)
                # max_turns > 1: the SDK spends a turn emitting structured output,
                # so 1 always dies with error_max_turns even for a tool-free worker
                verdict = await run_agent(
                    WORKER, prompt, schema=SCHEMAS.get(rb.schema, VERDICT_SCHEMA),
                    with_tools=False, max_turns=4,
                    model=cfg.get("worker_model"), label=label, timeout=rb.timeout_s)
                status, error = verdict["status"], None
                break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                error = str(e)[:500]
                log.warning("swarm job %s (%s on %s #%s) attempt %d/%d errored: %s",
                            job_id, rb.name, target["kind"], target["id"],
                            attempt + 1, attempts, error)
                if attempt + 1 < attempts:  # stuck -> unit drops to the blocked zone
                    _emit_phase(run_id, job_id, "blocked",
                                f"stuck: {error[:80]} — retrying", attempt + 1,
                                ref=ref, runbook=rb.name)
        db.execute(
            "UPDATE swarm_jobs SET status=?, verdict=?, error=?, cost_usd=?, "
            "duration_ms=?, finished_at=? WHERE id=?",
            (status, json.dumps(verdict, ensure_ascii=False) if verdict else None, error,
             _claim_cost(label, cost_after), int((time.monotonic() - t0) * 1000),
             db.now(), job_id))
        db.audit("swarm_verdict",
                 {"run_id": run_id, "job_id": job_id, "kind": target["kind"],
                  "target": f"{target['project']}#{target['id']}",
                  "runbook": rb.name, "status": status, "verdict": verdict,
                  "error": error}, actor="swarm")
        _emit_job(job_id)


async def _pr_diff(repo: str, number: int, cap: int) -> str:
    proc = await asyncio.create_subprocess_exec(
        "gh", "pr", "diff", str(number), "--repo", repo,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0:
        return f"(diff unavailable: {err.decode()[:200]})"
    text = out.decode(errors="replace")
    return text[:cap] + ("\n... (diff truncated)" if len(text) > cap else "")


# ---- code phase: coder workers in isolated worktrees ---------------------------

async def _code_phase(run_id: int, profile: str, jobs: list[dict], cfg: dict) -> list[dict]:
    """ticket: spawn a coder per codeable ticket (from ticket-code-plan verdicts).
    tech_debt: spawn a coder per repo over unaddressed pr_review tech-debt
    findings. Every coder works in a fresh git worktree and stops at 'ready' —
    push + draft PR only happen after the user approves on the board (HITL).
    Self-healing: a failed coder gets one repair pass in the same worktree."""
    from . import worktrees
    code_cfg = cfg.get("code") or {}
    cap = int(code_cfg.get("max_worktrees", 2))
    tasks: list[tuple[dict, str]] = []  # (worktree_row, coder task text)

    if profile == "ticket":
        repos_map = code_cfg.get("project_repos") or {}
        for j in jobs:
            if len(tasks) >= cap or j["runbook"] != "ticket-code-plan" or not j["verdict"]:
                continue
            v = json.loads(j["verdict"])
            repo = repos_map.get(j["item_project"])
            if not (v.get("codeable") and v.get("change_plan") and repo):
                continue
            wt = await worktrees.create_worktree(
                repo, f"ab{j['item_id']}-r{run_id}", run_id=run_id, job_id=j["id"],
                kind="ticket", title=j["item_title"] or f"AB#{j['item_id']}",
                target_ref=f"AB#{j['item_id']}")
            if wt:
                await _flip_active(j["item_id"], j["item_project"])
                tasks.append((wt, (
                    f"Implement ADO work item AB#{j['item_id']}: {j['item_title']}\n\n"
                    f"Change plan (from the planning worker):\n{v['change_plan']}\n\n"
                    f"Reference AB#{j['item_id']} in your commit message.")))

    elif profile == "tech_debt":
        findings = _unaddressed_debt(cap_per_repo=3)
        for repo, items in list(findings.items())[:cap]:
            wt = await worktrees.create_worktree(
                repo, f"debt-r{run_id}", run_id=run_id, job_id=None,
                kind="tech_debt", title=f"tech debt: {items[0]['title'][:60]}",
                target_ref="; ".join(i["title"][:50] for i in items))
            if wt:
                lines = "\n".join(f"- {i['title']} (in {i['where']}): {i['why']}"
                                  for i in items)
                tasks.append((wt, (
                    "Fix the following tech-debt findings from PR reviews in this "
                    f"repo. Address each with a minimal, safe change:\n\n{lines}\n\n"
                    "Use one commit per finding if practical.")))

    # coders run concurrently — each has its own worktree, nothing waits
    return list(await asyncio.gather(*[_code_one(run_id, profile, wt, task, cfg)
                                       for wt, task in tasks]))


async def _flip_active(item_id: int, project: str) -> None:
    """Pipeline stage rule (owner rule): scan..merge -> the ticket is Active.
    Called when dev work actually starts; idempotent via ado_state_update."""
    from . import actions
    try:
        res = await actions.ado_state_update(
            item_id, "Active",
            project=project or (ado.configured_projects() or ["Your ADO Project"])[0],
            reason="swarm dev pipeline started on this ticket")
        if res != "already":
            log.info("swarm: ADO #%s -> Active (%s)", item_id, res)
    except Exception as e:
        log.warning("swarm: Active flip failed for #%s: %s", item_id, e)


async def _code_one(run_id: int, profile: str, wt: dict, task: str, cfg: dict) -> dict:
    """One coder end-to-end in an existing worktree: HITL clearance, coder run,
    repair loop (Fixing-lane gate), then the autonomous draft PR. Shared by the
    wave-mode _code_phase and the thread pipeline."""
    from . import worktrees
    code_cfg = cfg.get("code") or {}
    tkey = f"ado:{(wt.get('target_ref') or '')[3:]}" \
        if (wt.get("target_ref") or "").startswith("AB#") else wt.get("target_ref", "")
    if not await _wait_clearance(run_id, tkey):
        db.execute("UPDATE swarm_worktrees SET status='error', "
                   "error='stopped by the user before coding', updated_at=? WHERE id=?",
                   (db.now(), wt["id"]))
        return {"worktree_id": wt["id"], "repo": wt["repo"], "branch": wt["branch"],
                "ok": False, "error": "stopped by the user", "pr": None}
    if wt.get("job_id"):
        _emit_phase(run_id, wt["job_id"], "coding",
                    f"coder attempt 1 — implementing in {wt['branch']}")
    result = await worktrees.run_worker(wt, task, label=f"swarm:coder:{profile}")
    # self-heal loop: same worktree, failure context appended; the loop
    # count is the Fixing-lane gate on the board
    for rep in range(gates.gate_value("repair_attempts")):
        if result["ok"]:
            break
        if wt.get("job_id"):
            _emit_phase(run_id, wt["job_id"], "coding",
                        f"coder attempt {rep + 2} after: {(result['error'] or '')[:70]}")
        result = await worktrees.run_worker(
            wt, f"{task}\n\nA previous attempt failed with: {result['error']}. "
                "Diagnose what went wrong, fix it, and complete the task.",
            label=f"swarm:coder:{profile}:repair")
    pr = None
    if result["ok"] and code_cfg.get("auto_pr", True):
        # pipeline is autonomous: committed work ships as a draft PR on its
        # own (kill switch + dry run still gate; dry run leaves it committed)
        from . import actions
        rows = db.query("SELECT * FROM swarm_worktrees WHERE id=?", (wt["id"],))
        try:
            pr = (await actions.swarm_pr_open(rows[0]))if rows else None
        except actions.SendBlocked as e:
            pr = {"status": f"blocked: {e}"}
        except Exception as e:
            pr = {"status": f"error: {str(e)[:120]}"}
    return {"worktree_id": wt["id"], "repo": wt["repo"], "branch": wt["branch"],
            "ok": result["ok"], "error": result.get("error"), "pr": pr}


async def _thread_flow(run_id: int, targets: list[dict], cfg: dict,
                       concurrency: int | None) -> list[dict]:
    """The owner's thread architecture (diagram, 2026-07-09): incoming ticket queue
    -> dispatcher -> N worker threads. Each thread pulls ONE ticket and walks
    it end-to-end — intake/scanning (not ready -> parked; the clarify loop asks
    on the ticket and the next run re-enters it), then development (code plan
    -> worktree coder -> draft PR) — and only then pulls the next ticket. No
    cross-ticket barriers: one ticket can be opening its PR while another is
    still being scanned. Review/re-review/merge stages continue asynchronously
    via the heartbeat (ci_watch, pr_sync, announce_green); HITL stays before
    the final merge — the swarm never merges. Default 4 threads (swarm.threads
    in policy); the board's scale selection overrides."""
    from . import worktrees
    rbs = profile_runbooks("thread", cfg)
    inv = next((r for r in rbs if r.schema != "plan"), None)
    plan = next((r for r in rbs if r.schema == "plan"), None)
    ados = [t for t in targets if (t.get("kind") or "ado") == "ado"]
    if not (inv and plan and ados):
        return []
    model = cfg.get("worker_model") or WORKER.model
    repos_map = (cfg.get("code") or {}).get("project_repos") or {}

    def _insert_job(t: dict, rb: Runbook, thread: int | None = None) -> int:
        return db.execute(
            "INSERT INTO swarm_jobs(run_id,kind,item_id,item_title,item_type,"
            "item_state,item_project,item_url,runbook,model,thread) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, t["kind"], t["id"], t["title"], t["type"], t["state"],
             t["project"], t["url"], rb.name, model, thread))

    # the incoming queue: every ticket gets its intake job upfront so the
    # board shows the whole queue before the dispatcher hands anything out
    with db.transaction():
        pending = [(t, _insert_job(t, inv)) for t in ados]
    _emit_run(run_id)
    queue: asyncio.Queue = asyncio.Queue()
    for pair in pending:
        queue.put_nowait(pair)
    # auto-scale to the ticket load, hard-capped (owner rule: max 4 threads; fewer
    # tickets -> fewer threads; drained threads exit on their own)
    hard_cap = int(cfg.get("threads_max", 4))
    n_threads = max(1, min(int(concurrency or cfg.get("threads", 4)),
                           hard_cap, len(ados)))
    log.info("swarm run %s: thread pipeline — %d tickets over %d threads",
             run_id, len(ados), n_threads)
    sem = asyncio.Semaphore(n_threads)
    wts: list[dict] = []

    async def _thread(tid: int) -> None:
        while True:
            try:
                t, inv_jid = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            ref = f"#{t['id']}"
            db.execute("UPDATE swarm_jobs SET thread=? WHERE id=?", (tid, inv_jid))
            _emit_phase(run_id, inv_jid, "evidence",
                        f"thread {tid} picked up {ref}", ref=ref, runbook=inv.name)
            await _run_job(inv_jid, t, inv, sem, cfg, run_id)
            row = db.query("SELECT verdict, status FROM swarm_jobs WHERE id=?",
                           (inv_jid,))[0]
            v = json.loads(row["verdict"]) if row["verdict"] else {}
            if row["status"] in ("error",) or not v.get("workable"):
                continue  # not ready: parked; clarify asks, next run re-enters
            # ready for dev: the ticket is now being worked -> ADO state Active
            await _flip_active(t["id"], t["project"])
            # plan + code in the SAME thread, right now
            plan_jid = _insert_job(t, plan, thread=tid)
            _emit_job(plan_jid)
            await _run_job(plan_jid, t, plan, sem, cfg, run_id)
            prow = db.query("SELECT verdict FROM swarm_jobs WHERE id=?", (plan_jid,))[0]
            pv = json.loads(prow["verdict"]) if prow["verdict"] else {}
            repo = repos_map.get(t["project"])
            if not (pv.get("codeable") and pv.get("change_plan") and repo):
                continue  # plan says not codeable (or no repo mapping): parked
            wt = await worktrees.create_worktree(
                repo, f"ab{t['id']}-r{run_id}", run_id=run_id, job_id=plan_jid,
                kind="ticket", title=t["title"] or f"AB#{t['id']}",
                target_ref=f"AB#{t['id']}")
            if not wt:
                continue
            wts.append(await _code_one(run_id, "thread", wt, (
                f"Implement ADO work item AB#{t['id']}: {t['title']}\n\n"
                f"Change plan (from the planning worker):\n{pv['change_plan']}\n\n"
                f"Reference AB#{t['id']} in your commit message."), cfg))

    await asyncio.gather(*[_thread(i + 1) for i in range(n_threads)])
    return wts


def _unaddressed_debt(cap_per_repo: int = 3) -> dict[str, list[dict]]:
    """Tech-debt findings from pr_review verdicts that no worktree addresses yet."""
    taken: set[str] = set()
    for w in db.query("SELECT target_ref FROM swarm_worktrees WHERE kind='tech_debt'"):
        taken.update(t.strip() for t in (w["target_ref"] or "").split(";"))
    rows = db.query(
        "SELECT item_project, verdict FROM swarm_jobs "
        "WHERE runbook='pr-review' AND verdict IS NOT NULL ORDER BY id DESC LIMIT 50")
    by_repo: dict[str, list[dict]] = {}
    for r in rows:
        v = json.loads(r["verdict"])
        for f in v.get("tech_debt") or []:
            if f.get("title", "")[:50] in taken:
                continue
            bucket = by_repo.setdefault(r["item_project"], [])
            if len(bucket) < cap_per_repo and f not in bucket:
                bucket.append(f)
    # debt banked from PR review comments (the review pipeline's triage)
    for r in db.query("SELECT detail FROM audit_log WHERE action='swarm_review_debt' "
                      "ORDER BY id DESC LIMIT 30"):
        d = json.loads(r["detail"])
        for f in d.get("findings") or []:
            if f.get("title", "")[:50] in taken:
                continue
            bucket = by_repo.setdefault(d.get("repo", ""), [])
            if len(bucket) < cap_per_repo and f not in bucket:
                bucket.append(f)
    return by_repo


async def _review(jobs: list[dict], cfg: dict) -> str | None:
    """Optional stronger-model pass over all verdicts -> run summary markdown."""
    model = cfg.get("reviewer_model")
    if not model or not jobs:
        return None
    compact = [{
        "target": _label(j),
        "runbook": j["runbook"],
        "status": j["status"],
        **(json.loads(j["verdict"]) if j["verdict"] else {"error": j["error"]}),
    } for j in jobs]
    prompt = ("One swarm run just finished over the owner's ADO board. "
              "Worker verdicts:\n\n"
              f"{json.dumps(compact, ensure_ascii=False, indent=1)[:30000]}\n\n"
              "Write the run summary.")
    try:
        return await run_agent(REVIEWER, prompt, with_tools=False, max_turns=4,
                               model=model, label="swarm:reviewer", timeout=120)
    except Exception as e:
        log.warning("swarm reviewer pass failed: %s", e)
        return None


_CHIP = {"pass": "✅", "fail": "❌", "blocked": "🟡", "error": "⚠️"}


def _label(j: dict) -> str:
    """Human ref for a job's target: '#65885 title' (ADO) or 'repo#674 title' (PR)."""
    prefix = (f"{(j['item_project'] or '').split('/')[-1]}#{j['item_id']}"
              if j.get("kind") == "pr" else f"#{j['item_id']}")
    return f"{prefix} {(j['item_title'] or '')[:60]}"


async def _receipts(run_id: int, jobs: list[dict], totals: dict,
                    summary: str | None, cfg: dict) -> dict:
    """Outward receipts, best-effort: an evidence comment per ADO item / PR and
    one Teams run summary. All go through actions.py (kill switch + dry run)."""
    from . import actions
    rc = cfg.get("receipts") or {}
    out: dict = {"ado_comments": [], "pr_comments": [], "teams_summary": None}
    by_target: dict[tuple, list[dict]] = {}
    for j in jobs:
        by_target.setdefault((j.get("kind") or "ado", j["item_id"]), []).append(j)
    for (kind, item_id), tjobs in by_target.items():
        md = _item_comment_md(run_id, tjobs)
        ref = (f"{(tjobs[0]['item_project'] or '').split('/')[-1]}#{item_id}"
               if kind == "pr" else f"#{item_id}")
        _emit_phase(run_id, tjobs[0]["id"], "commenting",
                    f"posting evidence comment on {ref}", ref=ref)
        try:
            if kind == "pr":
                if not rc.get("pr_comment", True):
                    continue
                result = await actions.post_github_comment(
                    tjobs[0]["item_url"], md, item_id=None)
            else:
                if not rc.get("ado_comment", True):
                    continue
                digest = _comment_digest(tjobs)
                ok, gated_md, why = await _gate_comment(
                    run_id, "swarm_ado_comment", item_id,
                    tjobs[0]["item_project"], md, digest)
                if not ok:
                    db.audit("swarm_comment_skipped",
                             {"ado_item": item_id, "kind": "evidence", "reason": why},
                             actor="swarm")
                    result = f"skipped: {why}"
                    _emit_phase(run_id, tjobs[0]["id"], "idle", "", ref=ref)
                    out["ado_comments"].append({"item": item_id, "result": result})
                    continue
                result = await actions.ado_comment_post(
                    item_id, gated_md, project=tjobs[0]["item_project"],
                    digest=digest)
        except actions.SendBlocked as e:
            result = f"blocked: {e}"
        except Exception as e:
            result = f"error: {str(e)[:120]}"
        out["pr_comments" if kind == "pr" else "ado_comments"].append(
            {"item": item_id, "result": result})
        if kind == "pr" and rc.get("pr_chat", True):
            out.setdefault("pr_chat", []).append(
                await _pr_chat_notify(run_id, item_id, tjobs, ref))
        _emit_phase(run_id, tjobs[0]["id"], "idle", "", ref=ref)
    if rc.get("clarify_comment", True):
        out["clarifications"] = await _clarify(run_id, by_target)
    if jobs:
        try:
            out["teams_summary"] = await actions.swarm_summary_post(
                _summary_md(run_id, totals, jobs, summary))
        except actions.SendBlocked as e:
            out["teams_summary"] = f"blocked: {e}"
        except Exception as e:
            out["teams_summary"] = f"error: {str(e)[:120]}"
    return out


def _ado_mention(by_id: str, name: str) -> str:
    """A real ADO @mention (notifies the person) when we know the identity id,
    plain @Name otherwise."""
    if by_id:
        return f'<a href="#" data-vsts-mention="version:2.0,{by_id}">@{name}</a>'
    return f"@{name}"


async def _clarify_target(project: str, item_id: int) -> tuple[str, str]:
    """Who should answer the questions: the last human in the ticket's comment
    history who isn't the user (previous owner / the tester)."""
    me = (policy().get("me", {}).get("name") or "").strip().lower()
    try:
        comments = await asyncio.to_thread(ado.get_comments, project, item_id, 10)
    except Exception:
        return "", ""
    for c in comments:  # newest first
        who = (c.get("by") or "").strip()
        if not who or who.lower() == me:
            continue
        if (c.get("text") or "").lstrip().startswith("🤖"):
            continue
        return c.get("by_id") or "", who
    return "", ""


async def _clarify(run_id: int, by_target: dict) -> list[dict]:
    """Unclear acceptance criteria -> ask on the ticket, TAG the person who
    should answer (previous owner / tester, from the comment history), and
    wait. Posts ONE comment per item (7-day dedupe); work resumes on its own
    once someone answers on the ticket or in the UAT chat."""
    from . import actions
    out = []
    for (kind, item_id), tjobs in by_target.items():
        if kind != "ado":
            continue
        questions: list[str] = []
        for j in tjobs:
            v = json.loads(j["verdict"]) if j["verdict"] else {}
            if j["status"] != "pass":
                questions += [q for q in v.get("clarifying_questions") or [] if q]
        if not questions:
            continue
        asked = db.query(
            "SELECT 1 FROM audit_log WHERE action='swarm_clarify' "
            "AND json_extract(detail,'$.ado_item')=? AND ts>? "
            "AND json_extract(detail,'$.dry_run') IS NULL LIMIT 1",
            (item_id, db.cutoff(days=7)))
        if asked:
            out.append({"item": item_id, "result": "already asked, waiting for answers"})
            continue
        _emit_phase(run_id, tjobs[0]["id"], "commenting",
                    f"asking for clarification on #{item_id}", ref=f"#{item_id}")
        seen: list[str] = []
        for q in questions:
            if q not in seen:
                seen.append(q)
        # simple + conversational, addressed to the person who can answer
        by_id, who = await _clarify_target(tjobs[0]["item_project"], item_id)
        tag = _ado_mention(by_id, who) if who else ""
        opener = (f"🤖 {tag} — could you clarify "
                  f"{'a couple of things' if len(seen) > 1 else 'one thing'} "
                  "so the swarm can move this forward?"
                  if tag else
                  "🤖 Quick question so the swarm can move this forward:")
        md = (opener
              + "<ol>" + "".join(f"<li>{q}</li>" for q in seen[:4]) + "</ol>"
              + "<i>A short reply here or in the UAT chat is enough — "
                "work resumes automatically once answered.</i>")
        ok, md, why = await _gate_comment(
            run_id, "swarm_clarify", item_id, tjobs[0]["item_project"], md)
        if not ok:
            db.audit("swarm_comment_skipped",
                     {"ado_item": item_id, "kind": "clarify", "reason": why},
                     actor="swarm")
            _emit_phase(run_id, tjobs[0]["id"], "idle", "", ref=f"#{item_id}")
            out.append({"item": item_id, "result": f"skipped: {why}"})
            continue
        try:
            result = await actions.ado_clarify_post(
                item_id, md, project=tjobs[0]["item_project"])
        except actions.SendBlocked as e:
            result = f"blocked: {e}"
        except Exception as e:
            result = f"error: {str(e)[:120]}"
        _emit_phase(run_id, tjobs[0]["id"], "idle", "", ref=f"#{item_id}")
        out.append({"item": item_id, "questions": len(seen), "result": result})
    return out


def _comment_digest(tjobs: list[dict]) -> str:
    """Stable fingerprint of what a receipt comment would say."""
    parts = []
    for j in sorted(tjobs, key=lambda x: x["runbook"]):
        v = json.loads(j["verdict"]) if j["verdict"] else {}
        parts.append(f"{j['runbook']}={j['status']}:{v.get('headline', '')[:60]}")
    return "|".join(parts)


async def _gate_comment(run_id: int, kind_action: str, item_id: int, project: str,
                        proposed_html: str, digest: str | None = None) -> tuple[bool, str, str]:
    """Anti-spam guardrail for ticket comments. Returns (post?, html, reason).

    Layer 1 (free): within receipts.comment_cooldown_hours, skip when the
    digest matches the last posted comment (nothing changed).
    Layer 2 (smart): a tiny gate worker reads the ticket's existing comments
    and either skips, or rewrites the comment down to only the delta.
    """
    rc = policy().get("swarm", {}).get("receipts", {}) or {}
    cooldown = int(rc.get("comment_cooldown_hours", 24))
    last = db.query(
        "SELECT detail FROM audit_log WHERE action=? "
        "AND json_extract(detail,'$.ado_item')=? "
        "AND json_extract(detail,'$.dry_run') IS NULL AND ts>? "
        "ORDER BY id DESC LIMIT 1",
        (kind_action, item_id, db.cutoff(hours=cooldown)))
    if last and digest is not None:
        try:
            if json.loads(last[0]["detail"]).get("digest") == digest:
                return False, "", f"unchanged since last comment (<{cooldown}h)"
        except Exception:
            pass
    try:
        existing = await asyncio.to_thread(ado.get_comments, project, item_id, 12)
    except Exception:
        existing = []
    if not any("🤖" in (c.get("text") or "") for c in existing):
        return True, proposed_html, "no prior swarm comments"
    ctx = "\n".join(f"[{c['at']} {c['by']}] {c['text'][:500]}" for c in existing)
    try:
        verdict = await run_agent(
            GATE,
            f"# Existing comments on ADO #{item_id} (newest first)\n\n{ctx[:12000]}\n\n"
            f"# Proposed new swarm comment (HTML)\n\n{proposed_html[:6000]}",
            schema=COMMENT_GATE_SCHEMA, with_tools=False, max_turns=4,
            label="swarm:comment_gate", timeout=90)
    except Exception as e:
        log.warning("comment gate failed (%s); posting unmodified", e)
        return True, proposed_html, "gate errored, posted as-is"
    if verdict["action"] == "skip":
        return False, "", verdict.get("reason", "no new information")
    return True, verdict.get("comment_html") or proposed_html, verdict.get("reason", "")


async def _pr_chat_notify(run_id: int, number: int, tjobs: list[dict], ref: str) -> dict:
    """Write-back to the PR channel: post the review outcome and tag the PR
    owner so they can continue their work. Kill switch, dry run and the
    teams_write allowlist all apply (actions.pr_review_chat_post)."""
    from . import actions, graph
    _emit_phase(run_id, tjobs[0]["id"], "commenting",
                f"tagging the owner of {ref} in the PR channel", ref=ref)
    owner = ""
    d = await _gh_json("pr", "view", str(number), "--repo", tjobs[0]["item_project"],
                       "--json", "author")
    if d:
        owner = (d.get("author") or {}).get("login", "")
    v = json.loads(tjobs[0]["verdict"]) if tjobs[0]["verdict"] else {}
    crit = sum(len((json.loads(j["verdict"]) or {}).get("critical_issues") or [])
               for j in tjobs if j["verdict"])
    debt = sum(len((json.loads(j["verdict"]) or {}).get("tech_debt") or [])
               for j in tjobs if j["verdict"])
    msg = (f"🤖 Swarm review of {ref}"
           + (f" (@{owner})" if owner else "")
           + f": {v.get('headline') or tjobs[0]['status']}. "
           + f"{crit} critical, {debt} tech-debt"
           + (f" — details on the PR: {tjobs[0]['item_url']}" if tjobs[0]["item_url"] else ""))
    topic = (policy().get("pr_review", {}) or {}).get("chat_topic")
    if not topic:
        return {"item": number, "result": "skipped: no pr_review.chat_topic"}
    try:
        chat_id = next((c["id"] for c in await graph.chats_cached()
                        if (c.get("topic") or "").strip().lower() == topic.strip().lower()), None)
        if not chat_id:
            return {"item": number, "result": f"chat '{topic}' not found"}
        result = await actions.pr_review_chat_post(chat_id, msg, item_id=None)
    except actions.SendBlocked as e:
        result = f"blocked: {e}"
    except Exception as e:
        result = f"error: {str(e)[:120]}"
    return {"item": number, "result": result}


def _item_comment_md(run_id: int, item_jobs: list[dict],
                     fixes: list[dict] | None = None) -> str:
    """ADO comment body — SIMPLE and conversational (owner decision, 2026-07-10: no
    walls of evidence). One line per check, one next step, done."""
    lines, nxt = [], ""
    for j in item_jobs:
        v = json.loads(j["verdict"]) if j["verdict"] else {}
        head = v.get("headline") or j["error"] or j["status"]
        lines.append(f"{_CHIP.get(j['status'], '•')} {head}")
        if j["status"] != "pass" and not nxt and v.get("suggested_next_action"):
            nxt = v["suggested_next_action"]
    body = f"🤖 Run #{run_id} — " + "<br>".join(lines)
    if nxt:
        body += f"<br><i>Next: {nxt}</i>"
    for f in fixes or []:
        body += f"<br>🔧 {f['field']} → {f['value']} ({f['result']})"
    return body


def _summary_md(run_id: int, totals: dict, jobs: list[dict], summary: str | None) -> str:
    head = (f"🤖 Swarm run #{run_id}: {totals['pass']} pass, {totals['fail']} fail, "
            f"{totals['blocked']} blocked across {totals['items']} items "
            f"(cost ${totals['cost_usd']:.2f})")
    rows = [f"| {_label(j)[:52]} | {j['runbook']} | "
            f"{_CHIP.get(j['status'], '•')} {j['status']} |" for j in jobs]
    table = "\n".join(["| target | runbook | verdict |", "|---|---|---|"] + rows)
    return head + "\n\n" + table + (f"\n\n{summary}" if summary else "")


def _fail_run(run_id: int, error: str) -> None:
    db.execute("UPDATE swarm_runs SET finished_at=?, status='error', error=? WHERE id=?",
               (db.now(), error, run_id))
    _emit_run(run_id)


def _reap_orphans() -> None:
    """Self-healing housekeeping: a backend restart mid-run leaves 'running'
    rows behind — close them out; worktree rows whose directory vanished are
    marked discarded so the board never shows ghosts."""
    import os
    db.execute("UPDATE swarm_runs SET status='error', "
               "error='orphaned (backend restarted mid-run)', finished_at=? "
               "WHERE status='running' AND started_at<?",
               (db.now(), db.cutoff(minutes=30)))
    db.execute("UPDATE swarm_jobs SET status='error', error='orphaned' "
               "WHERE status IN ('queued','running') AND run_id IN "
               "(SELECT id FROM swarm_runs WHERE error LIKE 'orphaned%')")
    for w in db.query("SELECT id, path FROM swarm_worktrees "
                      "WHERE status IN ('active','ready')"):
        if not os.path.isdir(w["path"]):
            db.execute("UPDATE swarm_worktrees SET status='discarded', "
                       "error='worktree directory vanished', updated_at=? WHERE id=?",
                       (db.now(), w["id"]))
    db.execute("UPDATE swarm_worktrees SET status='error', "
               "error='orphaned (backend restarted mid-run)', updated_at=? "
               "WHERE status='active' AND created_at<?",
               (db.now(), db.cutoff(hours=2)))


# HITL intervention: board-started runs are cancellable mid-flight, and both
# the whole run and individual tickets can be paused/resumed/stopped. Pause
# takes effect between workers (a mid-flight LLM call finishes first).
_active_runs: dict[int, asyncio.Task] = {}
_run_ctl: dict[int, dict] = {}  # run_id -> {gate: Event(set=go), paused: set, stopped: set}


def _ctl(run_id: int) -> dict:
    c = _run_ctl.get(run_id)
    if not c:
        ev = asyncio.Event()
        ev.set()
        c = {"gate": ev, "paused": set(), "stopped": set()}
        _run_ctl[run_id] = c
    return c


def control_state(run_id: int) -> dict:
    c = _run_ctl.get(run_id)
    return {"paused": bool(c and not c["gate"].is_set()),
            "paused_targets": sorted(c["paused"]) if c else [],
            "stopped_targets": sorted(c["stopped"]) if c else []}


def control(run_id: int, action: str, target: str | None = None) -> dict:
    """Board intervention. target is 'ado:76053' / 'pr:686' or None for global."""
    c = _ctl(run_id)
    if target:
        if action == "pause":
            c["paused"].add(target)
        elif action == "resume":
            c["paused"].discard(target)
        elif action == "stop":
            c["stopped"].add(target)
            c["paused"].discard(target)
    else:
        if action == "pause":
            c["gate"].clear()
        elif action == "resume":
            c["gate"].set()
    db.audit("swarm_control", {"run_id": run_id, "action": action, "target": target},
             actor="user")
    state = control_state(run_id)
    events.broadcast("swarm_control", {"run_id": run_id, **state})
    return state


async def _wait_clearance(run_id: int, tkey: str) -> bool:
    """Block while paused; False when this target was stopped."""
    c = _ctl(run_id)
    while True:
        await c["gate"].wait()
        if tkey in c["stopped"]:
            return False
        if tkey not in c["paused"]:
            return True
        await asyncio.sleep(0.8)


def stop_run(run_id: int) -> bool:
    t = _active_runs.get(run_id)
    if t and not t.done():
        t.cancel()
        return True
    return False


async def _uat_chat_mentions(item_id: int, since: str | None = None) -> list[dict]:
    """Human messages in the UAT team chat that mention this ticket. Answers
    given there count exactly like ticket comments (owner decision, 2026-07-10)."""
    from . import graph
    chat_id = (policy().get("swarm", {}).get("receipts", {}) or {}).get("uat_chat_id")
    if not chat_id:
        return []
    try:
        msgs = await graph.read_chat_messages(chat_id, top=40)
    except Exception as e:
        log.warning("uat chat read failed: %s", e)
        return []
    out = []
    for m in msgs:
        body = re.sub(r"<[^>]+>", " ",
                      str((m.get("body") or {}).get("content", "") or "")).strip()
        if not body or body.startswith("🤖") or str(item_id) not in body:
            continue
        at = (m.get("createdDateTime") or "").replace("Z", "+00:00")
        if since and at[:16] <= since[:16]:
            continue
        who = ((m.get("from") or {}).get("user") or {}).get("displayName") or "teams"
        out.append({"who": who, "at": at, "text": body[:600]})
    out.sort(key=lambda x: x["at"])
    return out


_UAT_BUG_TITLE_RE = re.compile(r"tc-uat|^\s*\[uat\]", re.I)


def _is_uat_bug(title: str) -> bool:
    """UAT chat tracks the UAT bug tracker only (titles like '[UAT][A] TC-UAT-004
    — ...'); other swarm tickets (features, non-UAT bugs) don't belong there
    (owner decision, 2026-07-14 — an unrelated ticket nudge once landed in the UAT chat)."""
    return bool(_UAT_BUG_TITLE_RE.search(title or ""))


async def clarify_escalate() -> list[dict]:
    """Owner decision (2026-07-10, after questions for two teammates went
    unseen): when clarifying questions on a ticket sit unanswered past
    receipts.clarify_ping_hours, ping the UAT team chat so people actually see
    them — they can answer there OR on the ticket (both are read). One ping per
    ask, tracked in the audit trail. Only tickets that are actually part of the
    UAT bug tracker (_is_uat_bug) go to that chat; everything else nudges the
    user privately in the companion chat instead."""
    from . import actions
    rc = policy().get("swarm", {}).get("receipts", {}) or {}
    hours = float(rc.get("clarify_ping_hours", 24))
    cutoff = db.cutoff(minutes=int(hours * 60))
    asks = db.query(
        "SELECT json_extract(detail,'$.ado_item') AS item, MAX(ts) AS ts "
        "FROM audit_log WHERE action='swarm_clarify' "
        "AND json_extract(detail,'$.dry_run') IS NULL "
        "GROUP BY item ORDER BY ts DESC LIMIT 8")
    out = []
    for a in asks:
        if not a["item"] or a["ts"] > cutoff:
            continue  # give humans clarify_ping_hours before escalating
        item_id = int(a["item"])
        pinged = db.query(
            "SELECT 1 FROM audit_log WHERE action='swarm_uat_chat' "
            "AND json_extract(detail,'$.kind')='clarify_ping' "
            "AND json_extract(detail,'$.ado_item')=? AND ts>? LIMIT 1",
            (item_id, a["ts"]))
        if pinged:
            continue
        items = await asyncio.to_thread(ado.items_by_ids, [item_id])
        if not items:
            continue
        it = items[0]
        if it.get("bucket") in ("Done", "Removed"):
            continue  # ticket closed since; nothing to chase
        comments = await asyncio.to_thread(ado.get_comments, it["project"], item_id, 10)
        answered = any(
            c for c in comments
            if (c.get("at") or "") > a["ts"][:16] and not (c.get("text") or "").startswith("🤖"))
        answer_src = "ticket comment" if answered else (
            "UAT chat reply" if await _uat_chat_mentions(item_id, since=a["ts"]) else None)
        if answer_src:
            # answered -> resume the work automatically (once per ask)
            resumed = db.query(
                "SELECT 1 FROM audit_log WHERE action='swarm_clarify_resume' "
                "AND json_extract(detail,'$.ado_item')=? AND ts>? LIMIT 1",
                (item_id, a["ts"]))
            if not resumed and not db.query(
                    "SELECT 1 FROM swarm_runs WHERE status='running' LIMIT 1"):
                rid = start_run(trigger="auto:answered", item_ids=[item_id],
                                profile="thread")
                db.audit("swarm_clarify_resume",
                         {"ado_item": item_id, "run_id": rid, "source": answer_src},
                         actor="swarm")
                out.append({"item": item_id, "resumed": f"run #{rid} ({answer_src})"})
                log.info("swarm: #%s answered via %s -> resumed in run #%s",
                         item_id, answer_src, rid)
            continue
        who = it.get("assigned_to") or "team"
        msg = (f"🤖 {who}, quick nudge - AB#{item_id} ({it['title'][:80]}) is "
               f"parked on a couple of questions the swarm left in the ticket "
               f"comments on {a['ts'][:10]}. A short answer there or right here "
               f"works - it picks the work back up automatically once you reply. "
               f"{it['url']}")
        try:
            if rc.get("uat_chat_id") and _is_uat_bug(it["title"]):
                res = await actions.swarm_uat_chat_post(msg, kind="clarify_ping", item_id=item_id)
            else:
                # not a UAT-tracker bug - the UAT team chat isn't the place for
                # it; nudge the user privately instead, tagged the same way so the
                # dedupe query above still recognizes this ask as pinged.
                res = await actions.companion_reply(msg)
                db.audit("swarm_uat_chat", {"kind": "clarify_ping", "ado_item": item_id,
                                            "routed_to": "companion"}, actor="swarm")
        except actions.SendBlocked as e:
            res = f"blocked: {e}"  # a blocked chat must not fail the heartbeat
        out.append({"item": item_id, "ping": res})
        log.info("swarm clarify escalation for #%s: %s", item_id, res)
    return out


async def retry_stage(item_id: int, stage: str) -> dict:
    """Targeted retry from a chosen pipeline stage (the board's retry
    dropdown): scan/ready re-enter the thread pipeline from intake; trees/code
    spin a fresh worktree + coder from the last change plan (falling back to
    intake if there is none); prs ships a committed worktree; review/re-review
    drive the review stage now (re-review resets the comment cursor so ALL
    feedback is re-read); merge re-syncs the PR state with GitHub."""
    from . import actions
    from . import worktrees as _wt
    cfg = policy().get("swarm", {})
    ref = f"AB#{item_id}"
    stage = stage.replace("-", "").lower()
    db.audit("swarm_retry", {"item": item_id, "stage": stage}, actor="user")
    if stage in ("scan", "ready"):
        rid = start_run(trigger=f"retry:{stage}", item_ids=[item_id], profile="thread")
        return {"ok": True, "note": f"re-entered the pipeline from intake (run #{rid})"}
    if stage in ("trees", "code"):
        rows = db.query(
            "SELECT * FROM swarm_jobs WHERE runbook='ticket-code-plan' AND item_id=? "
            "AND verdict IS NOT NULL ORDER BY id DESC LIMIT 1", (item_id,))
        pv = json.loads(rows[0]["verdict"]) if rows else {}
        if not (pv.get("codeable") and pv.get("change_plan")):
            rid = start_run(trigger=f"retry:{stage}", item_ids=[item_id], profile="thread")
            return {"ok": True,
                    "note": f"no change plan yet — re-entered from intake (run #{rid})"}
        j = rows[0]
        repo = (cfg.get("code") or {}).get("project_repos", {}).get(j["item_project"])
        if not repo:
            return {"ok": False, "note": f"no repo mapping for {j['item_project']}"}
        run_id = db.execute(
            "INSERT INTO swarm_runs(trigger, profile, started_at) VALUES(?,?,?)",
            (f"retry:{stage}", "thread", db.now()))
        _emit_run(run_id)

        async def _bg():
            try:
                wt = await _wt.create_worktree(
                    repo, f"ab{item_id}-r{run_id}", run_id=run_id, job_id=j["id"],
                    kind="ticket", title=j["item_title"] or ref, target_ref=ref)
                res = None
                if wt:
                    await _flip_active(item_id, j["item_project"])
                    res = await _code_one(run_id, "thread", wt, (
                        f"Implement ADO work item {ref}: {j['item_title']}\n\n"
                        f"Change plan (from the planning worker):\n{pv['change_plan']}\n\n"
                        f"Reference {ref} in your commit message."), cfg)
                db.execute(
                    "UPDATE swarm_runs SET finished_at=?, status='done', totals=? WHERE id=?",
                    (db.now(), json.dumps({"retry": stage,
                                           "worktrees": [res] if res else []}), run_id))
            except Exception as e:
                db.execute("UPDATE swarm_runs SET finished_at=?, status='error', error=? "
                           "WHERE id=?", (db.now(), str(e)[:300], run_id))
            _emit_run(run_id)

        asyncio.create_task(_bg())
        return {"ok": True, "note": f"fresh worktree + coder from the last plan (run #{run_id})"}
    rows = db.query("SELECT * FROM swarm_worktrees WHERE target_ref=? "
                    "AND status != 'discarded' ORDER BY id DESC LIMIT 1", (ref,))
    w = rows[0] if rows else None
    if not w:
        return {"ok": False, "note": "no worktree/PR for this ticket yet — retry from Scan instead"}
    if stage == "prs":
        if w["status"] != "ready":
            return {"ok": False, "note": f"worktree is '{w['status']}' — nothing committed to ship"}
        try:
            pr = await actions.swarm_pr_open(w)
            return {"ok": True, "note": f"PR opened: {(pr or {}).get('url') or pr}"}
        except actions.SendBlocked as e:
            return {"ok": False, "note": f"blocked: {e}"}
    if stage in ("review", "rereview"):
        if w["status"] != "pr_created":
            return {"ok": False, "note": f"no open PR (worktree is '{w['status']}')"}
        if stage == "rereview":
            # reset the cursor: the review stage re-reads EVERY comment
            db.execute("UPDATE swarm_worktrees SET review_cursor=NULL, updated_at=? "
                       "WHERE id=?", (db.now(), w["id"]))
            w = db.query("SELECT * FROM swarm_worktrees WHERE id=?", (w["id"],))[0]
        out = await _wt._review_stage(w)
        return {"ok": True, "note": f"review stage ran: {out}"}
    if stage == "merge":
        synced = await _wt.pr_sync()
        mine = next((s for s in synced if s.get("worktree_id") == w["id"]), None)
        return {"ok": True, "note": f"synced with GitHub: "
                f"{mine['pr_state'] if mine else 'no state change'}"}
    if stage in ("uat", "smoke"):
        if w["status"] != "merged":
            return {"ok": False, "note": f"PR not merged yet (worktree is '{w['status']}')"}
        ucfg = policy().get("swarm", {}).get("uat", {}) or {}
        if stage == "uat":
            db.execute("UPDATE swarm_worktrees SET uat_status=NULL, uat_note=NULL, "
                       "updated_at=? WHERE id=?", (db.now(), w["id"]))
        else:  # smoke: force the deploy gate open and test now
            db.execute("UPDATE swarm_worktrees SET uat_status='deployed', "
                       "updated_at=? WHERE id=?", (db.now(), w["id"]))
        results = await _wt.uat_watch()
        mine = [r for r in results if r.get("worktree_id") == w["id"]]
        note = " · ".join(str(r.get("uat") or r.get("smoke")) for r in mine) or "checked"
        return {"ok": True, "note": note}
    return {"ok": False, "note": f"unknown stage '{stage}'"}


async def housekeeping_sweep() -> dict:
    """The pipeline's heartbeat (scheduler, every few minutes): no workers,
    just the autonomy plumbing — reap orphans/expired worktrees, ship
    committed work, watch CI and fix failures, announce green PRs."""
    from . import worktrees as _wt
    _reap_orphans()
    out: dict = {}
    out["expired"] = await _wt.reap_expired()
    shipped = await _wt.ship_ready()
    if shipped:
        out["shipped"] = shipped
    ci = await _wt.ci_watch()
    if ci:
        out["ci"] = ci
    synced = await _wt.pr_sync()
    if synced:
        out["pr_sync"] = synced
    uat = await _wt.uat_watch()
    if uat:
        out["uat"] = uat
    pings = await clarify_escalate()
    if pings:
        out["clarify_pings"] = pings
    announced = await _wt.announce_green()
    if announced:
        out["announced"] = announced
    return out or {"idle": True}


def start_run(trigger: str = "manual", item_ids: list[int] | None = None,
              profile: str = "board", pr_refs: list[dict] | None = None,
              concurrency: int | None = None) -> int:
    """Create the run row and launch the orchestrator in the background so the
    API returns the run id immediately; the board watches live over SSE."""
    run_id = db.execute(
        "INSERT INTO swarm_runs(trigger, profile, started_at) VALUES(?,?,?)",
        (trigger, profile, db.now()))

    async def _bg():
        from .health import tracked
        try:
            await tracked("swarm",
                          lambda: run_swarm(trigger=trigger, item_ids=item_ids,
                                            run_id=run_id, profile=profile,
                                            pr_refs=pr_refs, concurrency=concurrency),
                          trigger=trigger)
        except asyncio.CancelledError:
            pass  # run_swarm already recorded the stop
        except Exception as e:
            log.exception("swarm run %s failed", run_id)
            db.audit("job_error", {"job": "swarm", "run_id": run_id, "error": str(e)[:500]})

    task = asyncio.create_task(_bg())
    _active_runs[run_id] = task
    task.add_done_callback(lambda _t: _active_runs.pop(run_id, None))
    return run_id


async def sweep() -> dict:
    """Scheduler entry. Scheduled runs are off unless swarm.schedule_enabled;
    manual runs (board button / POST /api/swarm/run) are always available."""
    if not policy().get("swarm", {}).get("schedule_enabled", False):
        return {"skipped": "swarm.schedule_enabled is false"}
    return await run_swarm(trigger="schedule")


# ---- board queries -----------------------------------------------------------

def list_runs(limit: int = 20, profiles: list[str] | None = None) -> list[dict]:
    if profiles:
        ph = ",".join("?" * len(profiles))
        runs = db.query(
            f"SELECT * FROM swarm_runs WHERE COALESCE(profile,'board') IN ({ph}) "
            "ORDER BY id DESC LIMIT ?", (*profiles, min(limit, 100)))
    else:
        runs = db.query("SELECT * FROM swarm_runs ORDER BY id DESC LIMIT ?",
                        (min(limit, 100),))
    for r in runs:
        r["totals"] = json.loads(r["totals"]) if r["totals"] else None
    return runs


def get_run(run_id: int) -> dict | None:
    rows = db.query("SELECT * FROM swarm_runs WHERE id=?", (run_id,))
    if not rows:
        return None
    run = rows[0]
    run["totals"] = json.loads(run["totals"]) if run["totals"] else None
    jobs = db.query("SELECT * FROM swarm_jobs WHERE run_id=? ORDER BY id", (run_id,))
    for j in jobs:
        j["verdict"] = json.loads(j["verdict"]) if j["verdict"] else None
    return {"run": run, "jobs": jobs}


def _activity_line(action: str, d: dict) -> str:
    if action == "swarm_verdict":
        v = d.get("verdict") or {}
        return (f"{d.get('target') or '#' + str(d.get('ado_item', '?'))} · "
                f"{d.get('runbook')} → {d.get('status')}"
                + (f": {v.get('headline')}" if v.get("headline") else "")
                + (f" ({d.get('error', '')[:60]})" if d.get("error") else ""))
    if action == "swarm_run":
        return (f"run #{d.get('run_id')} finished · {d.get('pass', 0)} pass · "
                f"{d.get('fail', 0)} fail · {d.get('blocked', 0)} blocked · "
                f"${d.get('cost_usd', 0):.2f}")
    if action == "swarm_ado_comment":
        return (f"evidence comment → ADO #{d.get('ado_item')}"
                + (" (dry run)" if d.get("dry_run") else ""))
    if action == "swarm_summary_post":
        return "run summary → Teams" + (" (dry run)" if d.get("dry_run") else "")
    if action == "swarm_worktree_created":
        return f"worktree spun up: {d.get('branch')} for {d.get('target')}"
    if action == "swarm_worker_done":
        return (f"coder finished on {d.get('branch')} → {d.get('status')}"
                + (f" ({d.get('error', '')[:60]})" if d.get("error") else ""))
    if action == "swarm_worktree_discarded":
        return f"worktree discarded: {d.get('branch')}"
    if action == "swarm_pr_open":
        return (f"draft PR: {d.get('repo')} {d.get('branch')}"
                + (" (dry run)" if d.get("dry_run") else f" → {d.get('url', '')}"))
    if action == "swarm_stopped":
        return f"run #{d.get('run_id')} stopped by the user"
    if action == "swarm_comment_skipped":
        return (f"comment on ADO #{d.get('ado_item')} skipped ({d.get('kind')}): "
                f"{(d.get('reason') or '')[:70]}")
    if action == "swarm_clarify":
        return (f"asked for clarification on ADO #{d.get('ado_item')}"
                + (" (dry run)" if d.get("dry_run") else ""))
    if action == "swarm_ado_state":
        return (f"state → ADO #{d.get('ado_item')}: {d.get('state')}"
                + (" (already)" if d.get("already") else "")
                + (" (dry run)" if d.get("dry_run") else ""))
    if action == "swarm_ado_iteration":
        return (f"iteration → ADO #{d.get('ado_item')}: {d.get('iteration')}"
                + (" (already)" if d.get("already") else "")
                + (" (dry run)" if d.get("dry_run") else ""))
    if action == "github_comment":
        parts = (d.get("pr_url") or "").rstrip("/").split("/")
        ref = f"{parts[-3]}#{parts[-1]}" if len(parts) >= 3 else d.get("pr_url", "")
        return f"evidence comment → {ref}" + (" (dry run)" if d.get("dry_run") else "")
    return action


def activity(limit: int = 100, profiles: list[str] | None = None) -> list[dict]:
    """The receipts log: everything the swarm did, newest first, traceable back
    to its run. Sourced from the audit log (which is the source of truth)."""
    rows = db.query("SELECT id, ts, actor, action, detail FROM audit_log "
                    "WHERE action LIKE 'swarm%' OR action='github_comment' "
                    "ORDER BY id DESC LIMIT ?", (min(limit, 300),))
    run_prof = {r["id"]: (r["profile"] or "board")
                for r in db.query("SELECT id, profile FROM swarm_runs "
                                  "ORDER BY id DESC LIMIT 300")}
    out = []
    for r in rows:
        try:
            d = json.loads(r["detail"]) if r["detail"] else {}
        except Exception:
            d = {"raw": (r["detail"] or "")[:200]}
        run_id = d.get("run_id")
        prof = run_prof.get(run_id)
        # run-scoped events filter by profile; worktree/PR events always show
        if profiles and prof is not None and prof not in profiles:
            continue
        out.append({"id": r["id"], "ts": r["ts"], "actor": r["actor"],
                    "action": r["action"], "run_id": run_id, "profile": prof,
                    "line": _activity_line(r["action"], d)})
    return out


def board_state(profiles: list[str] | None = None, span: int = 10) -> dict:
    """The persistent board: units from the last `span` runs merged, each
    target showing its state from the newest run that touched it. Tickets
    stay on the board across runs instead of vanishing when a new run starts."""
    runs = list_runs(span, profiles)
    if not runs:
        return {"jobs": [], "runs": []}
    ids = [r["id"] for r in runs]
    ph = ",".join("?" * len(ids))
    rows = db.query(f"SELECT * FROM swarm_jobs WHERE run_id IN ({ph}) ORDER BY id",
                    tuple(ids))
    best: dict = {}
    for j in rows:
        k = (j["kind"] or "ado", j["item_id"])
        cur = best.get(k)
        if cur is None or j["run_id"] > cur["run_id"]:
            best[k] = {"run_id": j["run_id"], "jobs": [j]}
        elif j["run_id"] == cur["run_id"]:
            cur["jobs"].append(j)
    jobs = sorted((j for v in best.values() for j in v["jobs"]), key=lambda x: x["id"])
    for j in jobs:
        j["verdict"] = json.loads(j["verdict"]) if j["verdict"] else None
    return {"jobs": jobs, "runs": runs}


def target_history(kind: str, item_id: int) -> dict:
    """Everything the swarm ever did to one ticket/PR: every worker across every
    run (with its run's profile), plus all worktrees spun up for it."""
    jobs = db.query(
        "SELECT j.*, r.profile AS run_profile, r.started_at AS run_started FROM swarm_jobs j "
        "LEFT JOIN swarm_runs r ON r.id = j.run_id "
        "WHERE COALESCE(j.kind,'ado') = ? AND j.item_id = ? ORDER BY j.id DESC LIMIT 60",
        (kind, item_id))
    for j in jobs:
        j["verdict"] = json.loads(j["verdict"]) if j["verdict"] else None
    ids = [j["id"] for j in jobs]
    ph = ",".join("?" * len(ids)) or "NULL"
    wts = db.query(
        f"SELECT * FROM swarm_worktrees WHERE job_id IN ({ph}) OR target_ref = ? "
        "ORDER BY id DESC LIMIT 10",
        (*ids, f"AB#{item_id}" if kind == "ado" else f"#{item_id}"))
    return {"jobs": jobs, "worktrees": wts}


def latest(profiles: list[str] | None = None) -> dict | None:
    if profiles:
        ph = ",".join("?" * len(profiles))
        rows = db.query(
            f"SELECT id FROM swarm_runs WHERE COALESCE(profile,'board') IN ({ph}) "
            "ORDER BY id DESC LIMIT 1", tuple(profiles))
    else:
        rows = db.query("SELECT id FROM swarm_runs ORDER BY id DESC LIMIT 1")
    return get_run(rows[0]["id"]) if rows else None
