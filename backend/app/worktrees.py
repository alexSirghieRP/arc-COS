"""Git-worktree isolation + the coder worker harness for the swarm.

This is the run_worker() seam swarm.py promised: coding workers get Bash/
Write/Edit, but ONLY inside a throwaway git worktree of the target repo — the
checkout the human uses is never touched. Nothing here pushes or opens PRs:
a finished worktree lands in swarm_worktrees with status 'ready' and waits for
the user's approval on the board; the push + draft PR happen in
actions.swarm_pr_open, behind the kill switch and dry run like every other
outbound action.

Worktree lifecycle (swarm_worktrees.status):
  active -> ready -> pr_created            (approved on the board)
                  -> discarded             (rejected on the board)
         -> error                          (worker died / nothing committed)
"""

import asyncio
import json
import logging
import re
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from . import db, events, gates
from .config import policy

log = logging.getLogger("chief.worktrees")

WORKTREES_ROOT = Path(policy().get("swarm", {}).get("code", {}).get(
    "repos_dir", str(Path.home() / "projects"))) / ".swarm-worktrees"

CODER_PROMPT = (
    "You are a swarm coding worker operating in an ISOLATED git worktree (your "
    "working directory). Implement exactly the change described in the task, "
    "nothing more. Read the repo's CLAUDE.md / STANDARDS.md / README first if "
    "present and follow the house conventions. Keep the diff minimal and "
    "focused. Verify your change compiles/passes the nearest quick tests when "
    "practical. When done, stage and commit ALL your work with one clear "
    "conventional-commit message (git add -A && git commit -m ...). HARD "
    "RULES: never git push, never create PRs or comments, never modify git "
    "config or remotes, never touch paths outside your working directory, "
    "never run destructive commands. Your final message must be a 3-6 line "
    "summary of what you changed and how you verified it."
)


def _default_project() -> str:
    """Last-resort ADO project when a worktree row carries none."""
    from . import ado
    return (ado.configured_projects() or ["Your ADO Project"])[0]


def _repo_dir(repo_name: str) -> Path | None:
    root = Path(policy().get("swarm", {}).get("code", {}).get(
        "repos_dir", str(Path.home() / "projects")))
    p = root / repo_name.split("/")[-1]
    return p if (p / ".git").exists() else None


async def _git(cwd: str | Path, *args: str, check: bool = True) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(cwd), *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:3])}: {err.decode()[:300]}")
    return out.decode()


def _emit(wt_id: int) -> None:
    rows = db.query("SELECT * FROM swarm_worktrees WHERE id=?", (wt_id,))
    if rows:
        events.broadcast("worktree_update", {"worktree": rows[0]})


async def create_worktree(repo_name: str, slug: str, *, run_id: int,
                          job_id: int | None, kind: str, title: str,
                          target_ref: str) -> dict | None:
    """git worktree add on a fresh swarm/<slug> branch off the repo's base.
    Returns the swarm_worktrees row, or None when the repo isn't cloned locally."""
    repo_dir = _repo_dir(repo_name)
    if repo_dir is None:
        log.warning("swarm code: repo %s not found locally", repo_name)
        return None
    code_cfg = policy().get("swarm", {}).get("code", {})
    base = code_cfg.get("base_branch", "dev")
    branch = f"swarm/{slug}"
    path = WORKTREES_ROOT / repo_dir.name / slug
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        await _git(repo_dir, "fetch", "origin", base)
    except Exception as e:
        log.warning("swarm code: fetch failed (%s); branching from local %s", e, base)
    await _git(repo_dir, "worktree", "add", "-b", branch, str(path), f"origin/{base}")
    wt_id = db.execute(
        "INSERT INTO swarm_worktrees(run_id,job_id,kind,repo,branch,path,base_ref,"
        "target_ref,title,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, job_id, kind, repo_name, branch, str(path), base,
         target_ref, title[:200], "active", db.now(), db.now()))
    db.audit("swarm_worktree_created",
             {"worktree_id": wt_id, "repo": repo_name, "branch": branch,
              "path": str(path), "target": target_ref}, actor="swarm")
    _emit(wt_id)
    return db.query("SELECT * FROM swarm_worktrees WHERE id=?", (wt_id,))[0]


async def run_worker(wt: dict, task: str, *, label: str) -> dict:
    """One coding worker in one worktree. Returns {ok, summary, diff_stat}.
    The worker gets write tools; isolation comes from cwd + the branch, and the
    outbound seams (push/PR) stay in actions.py."""
    code_cfg = policy().get("swarm", {}).get("code", {})
    model = code_cfg.get("model", "claude-sonnet-4-6")
    timeout = float(code_cfg.get("timeout_seconds", 900))
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=CODER_PROMPT,
        cwd=wt["path"],
        allowed_tools=["Bash", "Read", "Write", "Edit", "Glob", "Grep"],
        disallowed_tools=["WebSearch", "WebFetch"],
        permission_mode="bypassPermissions",
        max_turns=int(code_cfg.get("max_turns", 60)),
        setting_sources=[],
    )
    summary, error = None, None
    try:
        async with asyncio.timeout(timeout):
            async for message in query(prompt=task, options=options):
                if isinstance(message, ResultMessage):
                    if message.subtype == "success":
                        summary = message.result
                    else:
                        error = f"coder run failed: {message.subtype}"
                    try:
                        from . import cost
                        cost.record_run(label, model,
                                        getattr(message, "total_cost_usd", 0.0),
                                        getattr(message, "usage", None),
                                        ok=error is None, error=error)
                    except Exception:
                        pass
    except TimeoutError:
        error = f"coder timed out after {timeout:.0f}s"
    except Exception as e:
        error = str(e)[:300]

    committed = (await _git(wt["path"], "log", "--oneline",
                            f"{wt['base_ref']}..HEAD", check=False)).strip()
    if not committed and not error:
        # uncommitted work is still reviewable — commit it for the human
        dirty = (await _git(wt["path"], "status", "--porcelain", check=False)).strip()
        if dirty:
            await _git(wt["path"], "add", "-A")
            await _git(wt["path"], "commit", "-m",
                       f"swarm: {wt['title'][:60]} (auto-committed)", check=False)
            committed = "auto-committed"
        else:
            error = "worker finished without changing anything"
    diff_stat = (await _git(wt["path"], "diff", "--stat",
                            f"origin/{wt['base_ref']}...HEAD", check=False)).strip()
    status = "error" if error else "ready"
    db.execute(
        "UPDATE swarm_worktrees SET status=?, summary=?, diff_stat=?, error=?, "
        "updated_at=? WHERE id=?",
        (status, (summary or "")[:4000], diff_stat[:2000],
         error, db.now(), wt["id"]))
    db.audit("swarm_worker_done",
             {"worktree_id": wt["id"], "repo": wt["repo"], "branch": wt["branch"],
              "status": status, "error": error,
              "diff_stat": diff_stat[-500:]}, actor="swarm")
    _emit(wt["id"])
    return {"ok": not error, "summary": summary, "diff_stat": diff_stat, "error": error}


async def discard(wt_id: int, reason: str | None = None, actor: str = "user") -> dict:
    """Dump a worktree into the bin: remove the directory + branch (the repo
    itself is untouched); the row stays as the receipt of what was dumped why."""
    rows = db.query("SELECT * FROM swarm_worktrees WHERE id=?", (wt_id,))
    if not rows:
        raise ValueError("worktree not found")
    wt = rows[0]
    repo_dir = _repo_dir(wt["repo"])
    if repo_dir:
        await _git(repo_dir, "worktree", "remove", "--force", wt["path"], check=False)
        await _git(repo_dir, "branch", "-D", wt["branch"], check=False)
        await _git(repo_dir, "worktree", "prune", check=False)
    db.execute("UPDATE swarm_worktrees SET status='discarded', error=?, updated_at=? "
               "WHERE id=?", (reason, db.now(), wt_id))
    db.audit("swarm_worktree_discarded",
             {"worktree_id": wt_id, "branch": wt["branch"], "reason": reason}, actor=actor)
    _emit(wt_id)
    return {"ok": True}


async def ship_ready() -> list[dict]:
    """Pipeline self-healing (owner rule: "they should do it themselves"): committed
    worktrees that never shipped — e.g. the gates were closed when their coder
    finished — open their draft PRs as soon as the gates allow, then announce
    in the PR review chat. Runs in every swarm run's housekeeping."""
    from . import actions
    out = []
    for w in db.query("SELECT * FROM swarm_worktrees WHERE status='ready' ORDER BY id"):
        try:
            committed = (await _git(w["path"], "log", "--oneline",
                                    f"origin/{w['base_ref']}..HEAD", check=False)).strip()
            if not committed:
                await discard(w["id"], reason="nothing committed — empty diff, dumped",
                              actor="swarm")
                out.append({"worktree_id": w["id"], "branch": w["branch"],
                            "status": "discarded: empty diff"})
                continue
            r = await actions.swarm_pr_open(w)
            out.append({"worktree_id": w["id"], "branch": w["branch"],
                        "status": (r or {}).get("status"), "url": (r or {}).get("url")})
        except actions.SendBlocked as e:
            out.append({"worktree_id": w["id"], "branch": w["branch"],
                        "status": f"blocked: {e}"})
        except Exception as e:
            log.exception("ship_ready failed for %s", w["branch"])
            out.append({"worktree_id": w["id"], "branch": w["branch"],
                        "status": f"error: {str(e)[:120]}"})
        _emit(w["id"])
    return out


async def _gh_pr_checks(pr_url: str) -> tuple[str, list[str]]:
    """('green'|'failed'|'running', failing check names) for a PR."""
    proc = await asyncio.create_subprocess_exec(
        "gh", "pr", "checks", pr_url, "--json", "name,bucket,link",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    try:
        checks = json.loads(out.decode() or "[]")
    except Exception:
        return "running", []
    if not checks:
        return "green", []
    failing = [c.get("name", "?") for c in checks if c.get("bucket") in ("fail",)]
    pending = [c for c in checks if c.get("bucket") == "pending"]
    if failing:
        return "failed", failing
    if pending:
        return "running", []
    return "green", []


REVIEW_TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "must_fix": {"type": "array", "items": {"type": "string"},
                     "description": "review comments that block the PR and must be fixed now"},
        "tech_debt": {"type": "array", "items": {
            "type": "object",
            "properties": {"title": {"type": "string"}, "where": {"type": "string"},
                           "why": {"type": "string"}},
            "required": ["title", "where", "why"]},
            "description": "non-blocking suggestions to bank as follow-up tech debt"},
    },
    "required": ["must_fix", "tech_debt"],
}


async def _pr_review_state(pr_url: str, cursor: str | None) -> dict:
    """Review decision + comments newer than the cursor, via gh."""
    proc = await asyncio.create_subprocess_exec(
        "gh", "pr", "view", pr_url, "--json",
        "reviewDecision,comments,reviews,author",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, _ = await proc.communicate()
    try:
        d = json.loads(out.decode() or "{}")
    except Exception:
        return {"decision": "", "new_comments": []}
    author = (d.get("author") or {}).get("login", "")
    items = []
    for c in (d.get("comments") or []) + (d.get("reviews") or []):
        who = (c.get("author") or {}).get("login", "")
        at = c.get("createdAt") or c.get("submittedAt") or ""
        body = (c.get("body") or "").strip()
        if not body or who == author:
            continue  # the swarm/author's own posts aren't review feedback
        if cursor and at <= cursor:
            continue
        items.append({"who": who, "at": at, "body": body[:800]})
    items.sort(key=lambda x: x["at"])
    return {"decision": d.get("reviewDecision") or "", "new_comments": items}


async def ci_watch() -> list[dict]:
    """The pipeline's CI stage (owner rule: wait for checks; if errors, fix them).
    For every shipped PR: poll checks; on failure re-materialize the branch's
    worktree, spawn a fix coder, push the fix (--no-verify), and let CI rerun.
    Attempts are capped by the CI-fix gate on the board (ci_fix_attempts)."""
    from . import actions
    code_cfg = policy().get("swarm", {}).get("code", {})
    cap = gates.gate_value("ci_fix_attempts")
    out = []
    rows = db.query("SELECT * FROM swarm_worktrees WHERE status='pr_created' "
                    "AND pr_url IS NOT NULL AND COALESCE(ci_status,'') != 'green' "
                    "ORDER BY id DESC LIMIT 8")
    for w in rows:
        state, failing = await _gh_pr_checks(w["pr_url"])
        db.execute("UPDATE swarm_worktrees SET ci_status=?, updated_at=? WHERE id=?",
                   (state, db.now(), w["id"]))
        _emit(w["id"])
        entry = {"worktree_id": w["id"], "branch": w["branch"], "ci": state}
        if state == "green":
            entry["review"] = await _review_stage(w)
        if state == "failed":
            attempts = db.query(
                "SELECT COUNT(*) AS n FROM audit_log WHERE action='swarm_ci_fix' "
                "AND json_extract(detail,'$.pr_url')=?", (w["pr_url"],))[0]["n"]
            if attempts >= cap:
                entry["fix"] = f"gave up after {attempts} attempts"
                out.append(entry)
                continue
            db.audit("swarm_ci_fix",
                     {"pr_url": w["pr_url"], "branch": w["branch"],
                      "attempt": attempts + 1, "failing": failing[:6]}, actor="swarm")
            repo_dir = _repo_dir(w["repo"])
            if repo_dir is None:
                entry["fix"] = "repo not cloned"
                out.append(entry)
                continue
            db.execute("UPDATE swarm_worktrees SET ci_status='fixing', updated_at=? "
                       "WHERE id=?", (db.now(), w["id"]))
            _emit(w["id"])
            # re-materialize the branch in its worktree slot: clean the slot,
            # pick up any remote commits (humans may have pushed fixes too),
            # and SURFACE failures instead of handing the coder a missing dir
            import shutil
            shutil.rmtree(w["path"], ignore_errors=True)
            await _git(repo_dir, "worktree", "prune", check=False)
            await _git(repo_dir, "fetch", "origin", w["branch"], check=False)
            try:
                await _git(repo_dir, "worktree", "add", "--force", w["path"], w["branch"])
                await _git(w["path"], "reset", "--hard", f"origin/{w['branch']}", check=False)
            except Exception as e:
                db.execute("UPDATE swarm_worktrees SET ci_status='failed', updated_at=? "
                           "WHERE id=?", (db.now(), w["id"]))
                entry["fix"] = f"worktree re-materialize failed: {str(e)[:100]}"
                out.append(entry)
                _emit(w["id"])
                continue
            task = (f"CI checks FAILED on {w['pr_url']} (branch {w['branch']}): "
                    f"{', '.join(failing[:6])}.\n"
                    "Reproduce locally (run the relevant tests/linters), fix the "
                    "failures with the smallest correct change, and commit. "
                    "Do NOT push; do not change unrelated code.")
            result = await run_worker(w, task, label="swarm:coder:ci_fix")
            if result["ok"]:
                try:
                    await actions.swarm_pr_push_fix(w)
                    db.execute("UPDATE swarm_worktrees SET status='pr_created', "
                               "ci_status='running', updated_at=? WHERE id=?",
                               (db.now(), w["id"]))
                    entry["fix"] = "pushed, checks rerunning"
                except actions.SendBlocked as e:
                    entry["fix"] = f"push blocked: {e}"
            else:
                db.execute("UPDATE swarm_worktrees SET status='pr_created', "
                           "ci_status='failed', updated_at=? WHERE id=?",
                           (db.now(), w["id"]))
                entry["fix"] = f"fix coder failed: {(result.get('error') or '')[:80]}"
            # the branch lives on the remote; drop the local checkout again
            await _git(repo_dir, "worktree", "remove", "--force", w["path"], check=False)
            await _git(repo_dir, "worktree", "prune", check=False)
            _emit(w["id"])
        out.append(entry)
    return out


async def _teams_pr_feedback(w: dict, cursor: str | None) -> list[dict]:
    """Feedback about THIS PR posted in the Teams PR channel (owner rule: monitor
    the channel — requested changes there count like GitHub review comments).
    Matches messages mentioning the PR url/number, its branch, or its AB# ref,
    newer than the review cursor; the swarm's own 🤖 announcements are skipped."""
    from . import graph
    chat_id = (policy().get("swarm", {}).get("receipts", {}) or {}).get("pr_chat_id")
    if not chat_id:
        return []
    try:
        msgs = await graph.read_chat_messages(chat_id, top=30)
    except Exception as e:
        log.warning("teams pr feedback read failed: %s", e)
        return []
    pr_no = (w["pr_url"] or "").rstrip("/").split("/")[-1]
    needles = [s.lower() for s in (
        w["pr_url"], f"#{pr_no}", f"pull/{pr_no}", w["branch"],
        w.get("target_ref") or "") if s]
    out = []
    for m in msgs:
        body = re.sub(r"<[^>]+>", " ",
                      str((m.get("body") or {}).get("content", "") or "")).strip()
        if not body or body.startswith("🤖"):
            continue
        low = body.lower()
        if not any(n in low for n in needles):
            continue
        at = m.get("createdDateTime") or ""
        if cursor and at <= cursor:
            continue
        who = ((m.get("from") or {}).get("user") or {}).get("displayName") or "teams"
        out.append({"who": f"{who} (Teams)", "at": at, "body": body[:800]})
    out.sort(key=lambda x: x["at"])
    return out


async def _review_stage(w: dict) -> str:
    """PR review pipeline toward the owner's goal: an APPROVED PR with no requested
    changes and all checks green. Feedback comes from BOTH GitHub (review
    comments) and the Teams PR channel (messages mentioning this PR); new
    items are triaged (must-fix vs tech debt), must-fixes go to a fix coder
    and get pushed. ready_to_merge requires: quiet + green + reviewDecision
    APPROVED. Tech-debt suggestions are banked for the Debt Swarm."""
    from . import actions
    from .agents import run_agent
    from .swarm import GATE  # reuse a small tool-free agent def
    code_cfg = policy().get("swarm", {}).get("code", {})
    st = await _pr_review_state(w["pr_url"], w["review_cursor"])
    feedback = sorted(
        st["new_comments"] + await _teams_pr_feedback(w, w["review_cursor"]),
        key=lambda x: x["at"])
    if not feedback:
        if st["decision"] == "CHANGES_REQUESTED":
            return "waiting: changes requested, no actionable comments found"
        if st["decision"] != "APPROVED":
            # quiet and green is NOT enough — a human must approve the PR
            db.execute("UPDATE swarm_worktrees SET review_status='in_review', "
                       "updated_at=? WHERE id=? "
                       "AND COALESCE(review_status,'')='ready_to_merge'",
                       (db.now(), w["id"]))
            _emit(w["id"])
            return f"waiting for approval (decision: {st['decision'] or 'none yet'})"
        db.execute("UPDATE swarm_worktrees SET review_status='ready_to_merge', "
                   "updated_at=? WHERE id=? AND COALESCE(review_status,'') != 'ready_to_merge'",
                   (db.now(), w["id"]))
        _emit(w["id"])
        return "ready_to_merge"
    attempts = db.query(
        "SELECT COUNT(*) AS n FROM audit_log WHERE action='swarm_review_fix' "
        "AND json_extract(detail,'$.pr_url')=?", (w["pr_url"],))[0]["n"]
    if attempts >= gates.gate_value("review_fix_attempts"):
        db.execute("UPDATE swarm_worktrees SET review_status='needs_human', updated_at=? "
                   "WHERE id=?", (db.now(), w["id"]))
        _emit(w["id"])
        return f"needs_human: gave up after {attempts} fix rounds"
    ctx = "\n\n".join(f"[{c['at']} {c['who']}] {c['body']}" for c in feedback)
    try:
        triage = await run_agent(
            GATE, "Triage this PR feedback (GitHub review comments and Teams channel "
                  "messages about the PR). must_fix = blocking items the coder must "
                  "address now; tech_debt = non-blocking follow-ups. Ignore chatter "
                  "that requests nothing.\n\n" + ctx[:10000],
            schema=REVIEW_TRIAGE_SCHEMA, with_tools=False, max_turns=4,
            label="swarm:review_triage", timeout=90)
    except Exception as e:
        log.warning("review triage failed: %s", e)
        triage = {"must_fix": [c["body"][:200] for c in feedback], "tech_debt": []}
    if triage["tech_debt"]:
        db.audit("swarm_review_debt",
                 {"repo": w["repo"], "pr_url": w["pr_url"], "findings": triage["tech_debt"]},
                 actor="swarm")
    cursor = feedback[-1]["at"]
    if not triage["must_fix"]:
        db.execute("UPDATE swarm_worktrees SET review_cursor=?, updated_at=? WHERE id=?",
                   (cursor, db.now(), w["id"]))
        _emit(w["id"])
        return f"comments triaged: 0 blocking, {len(triage['tech_debt'])} banked as debt"
    db.audit("swarm_review_fix",
             {"pr_url": w["pr_url"], "branch": w["branch"], "attempt": attempts + 1,
              "must_fix": triage["must_fix"][:6]}, actor="swarm")
    db.execute("UPDATE swarm_worktrees SET review_status='fixing', updated_at=? WHERE id=?",
               (db.now(), w["id"]))
    _emit(w["id"])
    repo_dir = _repo_dir(w["repo"])
    if repo_dir is None:
        return "repo not cloned"
    import shutil
    shutil.rmtree(w["path"], ignore_errors=True)
    await _git(repo_dir, "worktree", "prune", check=False)
    await _git(repo_dir, "fetch", "origin", w["branch"], check=False)
    try:
        await _git(repo_dir, "worktree", "add", "--force", w["path"], w["branch"])
        await _git(w["path"], "reset", "--hard", f"origin/{w['branch']}", check=False)
    except Exception as e:
        db.execute("UPDATE swarm_worktrees SET review_status='in_review', updated_at=? "
                   "WHERE id=?", (db.now(), w["id"]))
        return f"worktree re-materialize failed: {str(e)[:100]}"
    fixes = "\n".join(f"- {m}" for m in triage["must_fix"])
    result = await run_worker(
        w, f"Address these PR review comments on {w['pr_url']} (branch {w['branch']}):\n"
           f"{fixes}\n\nMake the smallest correct changes and commit. Do NOT push.",
        label="swarm:coder:review_fix")
    outcome = "fix failed"
    if result["ok"]:
        try:
            await actions.swarm_pr_push_fix(w)
            outcome = "review fixes pushed, checks rerunning"
        except actions.SendBlocked as e:
            outcome = f"push blocked: {e}"
    db.execute("UPDATE swarm_worktrees SET status='pr_created', review_status='in_review', "
               "review_cursor=?, ci_status='running', updated_at=? WHERE id=?",
               (cursor, db.now(), w["id"]))
    await _git(repo_dir, "worktree", "remove", "--force", w["path"], check=False)
    await _git(repo_dir, "worktree", "prune", check=False)
    _emit(w["id"])
    return outcome


async def pr_sync() -> list[dict]:
    """Keep every shipped PR in sync with GitHub (owner rule: 'always in sync — if
    something is merged or we get a comment, we need to know and update ticket
    status'). Each heartbeat, for every worktree with an open PR:
    - MERGED on GitHub -> row becomes 'merged', ADO ticket gets a comment and
      its state moves (receipts.merge_states map; comment survives a rejected
      transition).
    - CLOSED unmerged -> row becomes 'closed', ADO ticket told a human closed
      it without merging.
    - still OPEN but already green -> green rows leave ci_watch's scope, so
      re-check here: checks gone red again (new pushes) drop ci_status back to
      'failed' for the next ci_watch fix pass, and the review stage re-runs so
      late reviewer comments still get triaged (cursor keeps it cheap)."""
    out = []
    rows = db.query("SELECT * FROM swarm_worktrees WHERE status='pr_created' "
                    "AND pr_url IS NOT NULL ORDER BY id DESC LIMIT 12")
    for w in rows:
        try:
            proc = await asyncio.create_subprocess_exec(
                "gh", "pr", "view", w["pr_url"], "--json", "state,mergedAt",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            o, _e = await proc.communicate()
            d = json.loads(o.decode() or "{}")
        except Exception as e:
            log.warning("pr_sync: gh view failed for %s: %s", w["pr_url"], e)
            continue
        state = (d.get("state") or "").upper()
        entry = {"worktree_id": w["id"], "branch": w["branch"], "pr_state": state}
        if state in ("MERGED", "CLOSED"):
            merged = state == "MERGED"
            db.execute("UPDATE swarm_worktrees SET status=?, review_status=?, "
                       "updated_at=? WHERE id=?",
                       ("merged" if merged else "closed",
                        "merged" if merged else "closed", db.now(), w["id"]))
            _emit(w["id"])
            entry["ticket"] = await _close_ticket_loop(w, merged=merged)
            log.info("swarm pr_sync: %s -> %s (%s)", w["branch"],
                     state.lower(), entry["ticket"])
        elif (w["ci_status"] or "") == "green":
            chk, _failing = await _gh_pr_checks(w["pr_url"])
            if chk != "green":
                db.execute("UPDATE swarm_worktrees SET ci_status=?, updated_at=? "
                           "WHERE id=?", (chk, db.now(), w["id"]))
                _emit(w["id"])
                entry["ci"] = f"regressed to {chk}"  # next ci_watch spawns the fix
            else:
                entry["review"] = await _review_stage(w)
        out.append(entry)
    return out


# When a PR merges, its ticket moves to the type's "done" state. Overridable
# per type via policy swarm.receipts.merge_states ({"Bug": "Resolved", ...}).
_MERGE_STATES = {"Bug": "Resolved", "User Story": "Resolved", "Task": "Closed"}


async def _close_ticket_loop(w: dict, *, merged: bool) -> str:
    """PR merged/closed on GitHub -> tell the ADO ticket and move its state."""
    from . import actions, ado
    ref = w.get("target_ref") or ""
    if not ref.startswith("AB#"):
        return "no ADO ticket linked"
    item_id = int(ref[3:])
    rows = db.query("SELECT item_project FROM swarm_jobs WHERE id=?",
                    (w.get("job_id"),)) if w.get("job_id") else []
    project = (rows[0]["item_project"] if rows else "") or _default_project()
    if merged:
        body = (f"🤖 <b>PR merged</b> — <a href=\"{w['pr_url']}\">{w['pr_url']}</a> "
                f"(branch <code>{w['branch']}</code>) was merged on GitHub.")
    else:
        body = (f"🤖 <b>PR closed without merging</b> — "
                f"<a href=\"{w['pr_url']}\">{w['pr_url']}</a> "
                f"(branch <code>{w['branch']}</code>). "
                "The change did not land; this ticket may need another look.")
    outcome = []
    # idempotent: a re-sync (row reset, manual rerun) must not repeat the
    # comment — the audit trail is the memory
    already = db.query(
        "SELECT 1 FROM audit_log WHERE action='swarm_ado_comment' "
        "AND json_extract(detail,'$.body') LIKE ? "
        "AND json_extract(detail,'$.dry_run') IS NULL LIMIT 1",
        (f"%PR {'merged' if merged else 'closed'}%{w['pr_url']}%",))
    if already:
        outcome.append("comment already posted")
    else:
        try:
            outcome.append(f"comment {await actions.ado_comment_post(item_id, body, project=project)}")
        except Exception as e:
            outcome.append(f"comment failed: {str(e)[:80]}")
    if merged:
        # Owner rule: the ticket flips to Resolved only when ALL of its swarm
        # PRs are merged — one ticket can hold several worktrees/PRs
        open_others = db.query(
            "SELECT COUNT(*) AS n FROM swarm_worktrees WHERE target_ref=? "
            "AND id<>? AND status IN ('active','ready','pr_created')",
            (ref, w["id"]))[0]["n"]
        if open_others:
            outcome.append(f"{open_others} other PR/worktree still open — state unchanged")
            return " · ".join(outcome)
        try:
            items = await asyncio.to_thread(ado.items_by_ids, [item_id])
            itype = items[0]["type"] if items else ""
            cfg_map = (policy().get("swarm", {}).get("receipts", {})
                       .get("merge_states") or {})
            target = cfg_map.get(itype) or _MERGE_STATES.get(itype) or "Resolved"
            if items and items[0]["state"] == target:
                outcome.append(f"state already {target}")
            else:
                res = await actions.ado_state_update(
                    item_id, target, project=project,
                    reason=f"all swarm PRs merged (last: {w['pr_url']})",
                    resolution_note=f"Fixed by merged PR: {w['pr_url']}")
                outcome.append(f"state -> {target} ({res})")
        except Exception as e:
            outcome.append(f"state move failed: {str(e)[:80]}")
    return " · ".join(outcome)


_SMOKE_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["pass", "fail"]},
        "headline": {"type": "string",
                     "description": "one line: what the UAT monitor shows after this deploy"},
    },
    "required": ["status", "headline"],
}


async def uat_watch() -> list[dict]:
    """Post-merge pipeline stages (the owner's diagram): check whether the merged
    change ARRIVED in UAT, then run a smoke test there. Deploy signal: the
    merge commit is an ancestor of origin/<swarm.uat.branch>; with no branch
    configured (or none found), fall back to assuming deployment
    swarm.uat.delay_minutes after the merge. Smoke: a read-only worker judges
    the OSCA live monitor's latest snapshots for NEW breakage after the
    deploy; the verdict lands on the ADO ticket."""
    cfg = policy().get("swarm", {}).get("uat", {}) or {}
    if not cfg.get("enabled", True):
        return []
    rows = db.query("SELECT * FROM swarm_worktrees WHERE status='merged' "
                    "AND COALESCE(uat_status,'') NOT IN "
                    "('smoke_pass','smoke_fail','skipped') ORDER BY id DESC LIMIT 10")
    out = []
    # reconcile: every change that reached UAT must have flipped its ticket to
    # Test in Progress — covers rows that deployed before this rule existed.
    # The audit trail is the memory; ado_state_update records 'already' too.
    for w in db.query("SELECT * FROM swarm_worktrees WHERE status='merged' "
                      "AND uat_status IN ('deployed','smoke_pass','smoke_fail') "
                      "AND target_ref LIKE 'AB#%' ORDER BY id DESC LIMIT 10"):
        item_id = int(w["target_ref"][3:])
        flipped = db.query(
            "SELECT 1 FROM audit_log WHERE action='swarm_ado_state' "
            "AND json_extract(detail,'$.ado_item')=? "
            "AND json_extract(detail,'$.state')='Test in Progress' "
            "AND json_extract(detail,'$.dry_run') IS NULL LIMIT 1", (item_id,))
        if flipped:
            continue
        note = await _flip_ticket(w["target_ref"], w.get("job_id"), "Test in Progress",
                                  f"change deployed to UAT ({w['pr_url']})")
        out.append({"worktree_id": w["id"], "branch": w["branch"], "ticket": note})
    for w in rows:
        try:
            if not w["uat_status"]:
                note = await _uat_deploy_check(w, cfg)
                out.append({"worktree_id": w["id"], "branch": w["branch"], "uat": note})
                fresh = db.query("SELECT * FROM swarm_worktrees WHERE id=?", (w["id"],))[0]
                if fresh["uat_status"] == "deployed":
                    # Owner rule: change arrived in UAT -> Test in Progress
                    out[-1]["ticket"] = await _flip_ticket(
                        w.get("target_ref"), w.get("job_id"), "Test in Progress",
                        f"change deployed to UAT ({w['pr_url']})")
                w = fresh
            if w["uat_status"] == "deployed":
                res = await _uat_smoke(w, cfg)
                out.append({"worktree_id": w["id"], "branch": w["branch"], "smoke": res})
        except Exception as e:
            log.warning("uat watch failed for %s: %s", w["branch"], e)
    return out


async def _flip_ticket(ref: str | None, job_id, state: str, reason: str) -> str:
    """Move the linked ADO ticket's state as the pipeline progresses."""
    from . import actions
    if not (ref or "").startswith("AB#"):
        return "no ticket linked"
    item_id = int(ref[3:])
    rows = db.query("SELECT item_project FROM swarm_jobs WHERE id=?",
                    (job_id,)) if job_id else []
    project = (rows[0]["item_project"] if rows else "") or _default_project()
    try:
        res = await actions.ado_state_update(item_id, state, project=project,
                                             reason=reason)
        if res != "already":
            log.info("swarm: ADO #%s -> %s (%s)", item_id, state, res)
        return f"{state} ({res})"
    except Exception as e:
        return f"state move failed: {str(e)[:80]}"


async def _uat_deploy_check(w: dict, cfg: dict) -> str:
    proc = await asyncio.create_subprocess_exec(
        "gh", "pr", "view", w["pr_url"], "--json", "mergeCommit,mergedAt",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    o, _ = await proc.communicate()
    try:
        d = json.loads(o.decode() or "{}")
    except Exception:
        return "no merge info from gh"
    sha = (d.get("mergeCommit") or {}).get("oid")
    branch = (cfg.get("branch") or "").strip()
    repo_dir = _repo_dir(w["repo"])
    if branch and sha and repo_dir:
        await _git(repo_dir, "fetch", "origin", branch, check=False)
        try:
            await _git(repo_dir, "merge-base", "--is-ancestor", sha, f"origin/{branch}")
            deployed, why = True, f"merge {sha[:7]} is on origin/{branch}"
        except Exception:
            deployed, why = False, f"merge {sha[:7]} not on origin/{branch} yet"
        if deployed:
            db.execute("UPDATE swarm_worktrees SET uat_status='deployed', uat_note=?, "
                       "updated_at=? WHERE id=?", (why, db.now(), w["id"]))
            _emit(w["id"])
        return why
    # no deploy signal configured: assume deployed a fixed delay after merge
    delay = int(cfg.get("delay_minutes", 30))
    merged_at = d.get("mergedAt")
    if merged_at:
        from datetime import datetime, timedelta, timezone
        t = datetime.fromisoformat(merged_at.replace("Z", "+00:00"))
        if datetime.now(timezone.utc) - t >= timedelta(minutes=delay):
            why = f"assumed deployed — {delay}m after merge (no uat.branch signal configured)"
            db.execute("UPDATE swarm_worktrees SET uat_status='deployed', uat_note=?, "
                       "updated_at=? WHERE id=?", (why, db.now(), w["id"]))
            _emit(w["id"])
            return why
        return f"waiting: assumes deployed {delay}m after merge"
    return "no mergedAt from gh"


async def _uat_smoke(w: dict, cfg: dict) -> str:
    """Judge UAT health after this deploy from the OSCA live monitor data.
    The baseline can already be red — only NEW breakage fails the smoke."""
    from . import actions
    from .agents import AgentDef, run_agent
    if (cfg.get("smoke") or "osca") == "none":
        db.execute("UPDATE swarm_worktrees SET uat_status='smoke_pass', "
                   "uat_note='smoke disabled (swarm.uat.smoke: none)', updated_at=? "
                   "WHERE id=?", (db.now(), w["id"]))
        _emit(w["id"])
        return "skipped"
    snaps = [dict(r) for r in db.query(
        "SELECT * FROM osca_snapshots ORDER BY id DESC LIMIT 2")]
    # osca_problems is keyed by a TEXT signature — it has no id column
    probs = [dict(r) for r in db.query(
        "SELECT * FROM osca_problems WHERE status='open' ORDER BY rowid DESC LIMIT 10")]
    if not snaps:
        db.execute("UPDATE swarm_worktrees SET uat_status='smoke_pass', "
                   "uat_note='no monitor data — smoke skipped', updated_at=? WHERE id=?",
                   (db.now(), w["id"]))
        _emit(w["id"])
        return "no monitor data"
    smoke_agent = AgentDef(
        name="uat_smoke",
        system_prompt=(
            "You are a UAT smoke tester. A code change just deployed to UAT. You get "
            "the live pipeline monitor's latest snapshots and open problems. The "
            "baseline may ALREADY be degraded — fail ONLY if there is breakage that "
            "plausibly appeared with this deploy (new problems, jumps in failures). "
            "Judge strictly from the data. No em dashes."))
    verdict = await run_agent(
        smoke_agent,
        (f"# Deployed change\n{w['title']} (branch {w['branch']}, {w['pr_url']})\n"
         f"Summary: {(w.get('summary') or '')[:600]}\n\n"
         f"# Monitor snapshots (newest first)\n{json.dumps(snaps, default=str)[:8000]}\n\n"
         f"# Open problems\n{json.dumps(probs, default=str)[:6000]}"),
        schema=_SMOKE_SCHEMA, with_tools=False, max_turns=4, label="swarm:uat-smoke")
    status = "smoke_pass" if verdict.get("status") == "pass" else "smoke_fail"
    note = (verdict.get("headline") or "")[:200]
    db.execute("UPDATE swarm_worktrees SET uat_status=?, uat_note=?, updated_at=? "
               "WHERE id=?", (status, note, db.now(), w["id"]))
    _emit(w["id"])
    ref = w.get("target_ref") or ""
    if ref.startswith("AB#"):
        import urllib.parse
        item_id = int(ref[3:])
        rows = db.query("SELECT item_project FROM swarm_jobs WHERE id=?",
                        (w.get("job_id"),)) if w.get("job_id") else []
        project = (rows[0]["item_project"] if rows else "") or _default_project()
        icon = "✅" if status == "smoke_pass" else "❌"
        try:
            await actions.ado_comment_post(
                item_id,
                f"🤖 <b>UAT smoke test</b> after <a href=\"{w['pr_url']}\">the merged PR</a>: "
                f"{icon} <b>{'PASS' if status == 'smoke_pass' else 'FAIL'}</b> — {note}",
                project=project)
        except Exception as e:
            log.warning("smoke comment failed for %s: %s", ref, e)
        if status == "smoke_pass":
            # short, well-formatted update for the UAT team chat (owner rule)
            pr_no = (w["pr_url"] or "").rstrip("/").split("/")[-1]
            ado_org = policy().get("azure_devops", {}).get(
                "org_url", "https://dev.azure.com/your-ado-org")
            item_url = (f"{ado_org}/"
                        f"{urllib.parse.quote(project)}/_workitems/edit/{item_id}")
            try:
                res = await actions.swarm_uat_chat_post(
                    (f"🤖 {ref} is looking good in UAT ✅ - the fix (PR #{pr_no}) "
                     f"deployed and the smoke test passed: {note}\n"
                     f"Ticket is in Test in Progress, ready for a validation pass "
                     f"whenever someone has a minute: {item_url}"),
                    kind="uat_verified", item_id=item_id)
                log.info("swarm uat chat update for %s: %s", ref, res)
            except Exception as e:
                log.warning("uat chat update failed for %s: %s", ref, e)
    log.info("swarm uat smoke: %s -> %s (%s)", w["branch"], status, note)
    return f"{status}: {note}"


async def announce_green() -> list[dict]:
    """Post to the PR review chat ONLY when a swarm PR is ready for review:
    all checks green. Flips the draft to 'ready' first, then announces once
    (idempotent via the swarm_pr_chat audit)."""
    from . import actions
    out = []
    rows = db.query("SELECT * FROM swarm_worktrees WHERE status='pr_created' "
                    "AND pr_url IS NOT NULL AND ci_status='green' "
                    "ORDER BY id DESC LIMIT 10")
    for w in rows:
        announced = db.query(
            "SELECT 1 FROM audit_log WHERE action='swarm_pr_chat' "
            "AND json_extract(detail,'$.pr_url')=? "
            "AND json_extract(detail,'$.dry_run') IS NULL LIMIT 1", (w["pr_url"],))
        if announced:
            continue
        try:
            proc = await asyncio.create_subprocess_exec(
                "gh", "pr", "ready", w["pr_url"],
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            await proc.communicate()  # best-effort: already-ready is fine
            res = await actions.swarm_pr_announce(w, w["pr_url"])
            out.append({"worktree_id": w["id"], "branch": w["branch"],
                        "status": f"announce: {res}"})
            _emit(w["id"])
        except Exception as e:
            log.exception("green announce failed for %s", w["branch"])
            out.append({"worktree_id": w["id"], "branch": w["branch"],
                        "status": f"announce error: {str(e)[:100]}"})
    return out


def ttl_hours() -> int:
    return int(policy().get("swarm", {}).get("code", {}).get("worktree_ttl_hours", 72))


async def reap_expired() -> int:
    """Self-healing housekeeping: a worktree nobody approved or discarded within
    the TTL gets dumped automatically (dir + branch removed, row kept in the bin
    with the expiry reason). Runs at the start of every swarm run."""
    ttl = ttl_hours()
    stale = db.query(
        "SELECT id FROM swarm_worktrees WHERE status IN ('ready','error','active') "
        "AND updated_at < ?", (db.cutoff(hours=ttl),))
    for row in stale:
        try:
            await discard(row["id"], reason=f"expired after {ttl}h without approval",
                          actor="swarm")
        except Exception:
            log.exception("expiry discard failed for worktree %s", row["id"])
    return len(stale)


def list_worktrees(limit: int = 50) -> list[dict]:
    return db.query("SELECT * FROM swarm_worktrees ORDER BY id DESC LIMIT ?",
                    (min(limit, 200),))
