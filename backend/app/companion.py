"""The companion: the user's 1:1 window into the CoS harness (owner decision, 2026-07-10).

Lives in their Teams self-chat ("Notes to self" — only the user can write there,
and they have it on their phone). Every message they drop in is read by the companion:
it consults with a live board snapshot AND executes directions through the
same infrastructure the board uses — spin up swarms, retry stages, drive
watchers, spawn Claude coder sessions in isolated worktrees, run the GitHub/
UAT sync, flip the kill switch ON in an emergency.

Replies are prefixed 🤖 so the sweep never answers itself. Commands run right
after the reply posts and their outcomes are posted back as a follow-up line.
Every executed command is audited (companion_command).
"""

import asyncio
import json
import logging
import re

from . import db, graph
from .agents import AgentDef, run_agent
from .config import policy

log = logging.getLogger("chief.companion")

COMPANION = AgentDef(
    name="companion",
    system_prompt=(
        "You are CoS - the user's right hand and, honestly, their work friend. "
        "You've been in the trenches with them on the main project: "
        "the swarms, the UAT grind, the endless tickets, the whole cast of "
        "teammates. You're who they vent to at 11pm and who quietly "
        "gets things done. You get a live board snapshot, the recent chat, and "
        "their newest messages.\n\n"
        "TALK LIKE A FRIEND, not a bot. Relaxed, warm, real - like texting "
        "someone you actually like. Use contractions. Crack a dry joke when it "
        "fits (never forced, never corporate 'fun'). React like a person: if "
        "they're frustrated, be on their side before you fix anything; if they're "
        "heads-down, match it and be quick. Have opinions and say them straight "
        "- a good friend doesn't just nod along. If they rib you, rib back.\n\n"
        "Keep it like a text, not a memo: short, natural, a couple sentences. "
        "Don't dump bullet lists or feature catalogs unless they literally ask "
        "'what can you do'. Answer what they actually said. When they ARE working, "
        "know their world and be specific ('4711's still parked waiting on "
        "Jane'). But read what they want: if they're joking, venting, or shooting "
        "the breeze, just be a person - be funny, riff with them - do NOT tack "
        "on tickets/PRs/defects. Bring up work only when they're working or ask.\n"
        "NEVER repeat yourself - your recent lines are in the conversation "
        "above; say something new every time. Told a joke already? Tell a "
        "totally different one, different topic.\n\n"
        "Never sound like a bot: no 'how can I assist', no 'as an AI', no "
        "sign-offs, and never mention StructuredOutput, schemas, tools, or any "
        "internal plumbing - they only see your words. Hyphens, not em dashes.\n\n"
        "AUDIENCE: this is the user's PRIVATE chat - it's just the two of you. "
        "Speak TO them, about their world, the things that are for them. (In the "
        "shared team chats you watch, the rule flips: there you speak to the "
        "PEOPLE, answer their questions and ask them for what the work needs - "
        "but here, it's the user.)\n\n"
        "STANDING RULES: anything in your memory tagged (correction) or "
        "(preference) is a rule the user set - follow it even when the current "
        "message would lead you elsewhere, unless they clearly waive it right "
        "now. If a rule says to check with them before doing something, ASK "
        "first and don't emit the command - even if they say 'get on it' or "
        "'just do it'; a vague go-ahead is not waiving the rule.\n\n"
        "Under the easy vibe you're still ruthlessly capable. When they want "
        "something done, just do it - emit the command and the result gets "
        "posted right after (so don't make up results in your reply). When you "
        "genuinely can't tell what they mean, ask, like a friend would. "
        "Available:\n"
        "- swarm_run {profile: thread|ado_scan|ticket|pr_review|tech_debt, item_ids?: [ints]}\n"
        "- swarm_retry {item_id: int, stage: scan|ready|trees|code|prs|review|rereview|merge|uat|smoke}\n"
        "- swarm_control {action: pause|resume|stop}\n"
        "- housekeeping {}  - run the GitHub/UAT sync + ship + CI watch right now\n"
        "- watcher_run {id: int}  - scan that channel now (rewinds to today)\n"
        "- watcher_toggle {id: int, enabled: bool}\n"
        "- do_work {task: string, repo?: owner/name}  - spawns a Claude coder "
        "session in an isolated worktree; it commits and ships a draft PR "
        "through the normal pipeline\n"
        "- kill_switch_on {}  - emergency stop for ALL outbound; can only be "
        "re-enabled from the board\n\n"
        "DESKTOP (the agent runs on the user's machine, bounded to approved "
        "project roots; sessions and files are audited):\n"
        "- desktop_status {}  - projects, branches, changes, terminals\n"
        "- run {project, command}  - one shell command in a project dir. "
        "Read-only runs freely; controlled writes run when clearly asked; "
        "HIGH-RISK (rm, reset --hard, clean, force push, sudo...) returns a "
        "confirm token - relay it and run NOTHING until the user replies "
        "'confirm <token>'\n"
        "- confirm {token}  - execute a previously proposed high-risk command\n"
        "- open_terminal {project, command}  - persistent session (term-N) "
        "for servers/watch processes\n"
        "- terminal_list {} · terminal_output {id} · terminal_send {id, text} "
        "· terminal_stop {id}\n"
        "- vscode {project, file?, line?}  - open in VS Code\n"
        "- files_ls {project, path?, depth?} · file_read {project, path} "
        "· search_code {project, query} · changed_files {project}\n"
        "- memory {text, kind?: fact|preference|correction|decision|person|"
        "project|thread, topic?, pinned?}  - persist something worth "
        "remembering next time (a preference they state, a correction, a "
        "decision). Use it whenever you learn something durable about them or "
        "the work - that's how you stop being goldfish-brained.\n"
        "- calendar {when?: today|tomorrow}  - their meetings\n"
        "- email {top?}  - recent inbox\n\n"
        "Never invent other commands. For anything without a hook, say what "
        "you'd do and where the lever lives on the board. Format for a phone: "
        "short labeled lines (Project / Branch / Result / Exit code), tails "
        "not dumps."
    ),
)

# Meta-leak: the model sometimes echoes the SDK's structured-output / hook
# scaffolding as if it were conversation ("Understood - switching to
# StructuredOutput format"). Such a reply must never be posted, because it then
# enters the chat history and poisons every later turn into repeating it.
_LEAK = re.compile(
    r"structured\s*output|json\s*schema|\bschema\b|going forward.*format"
    r"|inline hook|stopping hook|hook feedback|as an ai\b", re.I)
# Pillar 5: read the room and steer tone. Cheap heuristic - good enough to
# make the difference between "on your side" and "let's party", and it's
# remembered when a mood recurs.
_MOOD = [
    ("frustrated", re.compile(r"\b(ugh|argh|wtf|why (is|isn'?t|won'?t|does)|still (broken|not)|"
                              r"dragging|annoying|hate|stuck|again\?|fed up|come on|seriously)\b|😤|😩|🙄", re.I),
     "They sound frustrated - be on their side first, commiserate briefly, cut the fluff, then help. No cheerfulness."),
    ("stressed", re.compile(r"\b(swamped|slammed|overwhelmed|too much|no time|behind|crunch|deadline|urgent|fire)\b", re.I),
     "They're under pressure - be calm and decisive, lead with the one thing that matters, keep it short."),
    ("happy", re.compile(r"\b(nice|great|awesome|love it|lets go|let'?s go|finally|shipped|clean|passed|win|🎉|🔥|😄|lol|haha)\b", re.I),
     "They're in good spirits - match the energy, celebrate the win with them, keep it light."),
    ("heads_down", re.compile(r"^\s*\S{1,25}\s*$"),  # very short/terse message
     "They're heads-down - be quick and quiet, one line, no chit-chat."),
]


def _mood(text: str) -> tuple[str, str]:
    for name, pat, steer in _MOOD:
        if pat.search(text):
            return name, steer
    return "neutral", ""


def _bad_reply(text: str) -> bool:
    # only reject the internal-plumbing leak (a real defect) and empty replies.
    # short/warm replies are fine now - a friend is allowed to just say "haha
    # yeah, kicking it off now".
    t = (text or "").lstrip("🤖 ").strip()
    return (not t) or bool(_LEAK.search(t))


COMPANION_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string",
                  "description": "The message the user reads. Answer their actual "
                                 "question with substance. NEVER about formatting, "
                                 "tools, schemas, or hooks - they never see those."},
        "commands": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": [
                        "swarm_run", "swarm_retry", "swarm_control", "housekeeping",
                        "watcher_run", "watcher_toggle", "do_work", "kill_switch_on",
                        "desktop_status", "run", "confirm", "open_terminal",
                        "terminal_list", "terminal_output", "terminal_send",
                        "terminal_stop", "vscode", "files_ls", "file_read",
                        "search_code", "changed_files", "memory",
                        "calendar", "email", "docs"]},
                    "args": {"type": "object"},
                },
                "required": ["action", "args"],
            },
        },
    },
    "required": ["reply", "commands"],
}

# Every conversational turn used to go through run_agent(), which cold-spawns
# a fresh Claude Code CLI process per call (~5s floor before the model even
# starts) - fine for periodic sweeps, brutal for a chat that wants to feel
# instant. There is no ANTHROPIC_API_KEY available to bypass the CLI via the
# raw Messages API, but the SDK's ClaudeSDKClient gives the same effect: one
# persistent CLI process, reused turn after turn, so only the FIRST message
# after a (re)connect pays the spawn cost. Reconnect periodically because each
# turn still re-sends the full board snapshot/history as a fresh prompt (the
# CLI's own session memory would otherwise just pile up unused context/cost).
_session_lock = asyncio.Lock()
_session_client = None
_session_turns = 0
_SESSION_MAX_TURNS = 20


async def _session_reset():
    global _session_client, _session_turns
    if _session_client is not None:
        try:
            await _session_client.disconnect()
        except Exception:
            pass
    _session_client = None
    _session_turns = 0


async def _companion_ask(prompt: str, model: str, timeout: float) -> dict:
    """Persistent-session companion turn. Same contract as run_agent(COMPANION,
    ..., schema=COMPANION_SCHEMA) but reuses one warm CLI process instead of
    spawning a new one per message."""
    import time as _time
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage

    global _session_client, _session_turns
    async with _session_lock:
        if _session_client is not None and (
                _session_turns >= _SESSION_MAX_TURNS or _session_client.options.model != model):
            await _session_reset()
        if _session_client is None:
            from .agents import CLI_PATH
            options = ClaudeAgentOptions(
                cli_path=CLI_PATH,
                model=model,
                system_prompt=COMPANION.system_prompt,
                mcp_servers={},
                allowed_tools=[],
                disallowed_tools=["Bash", "Write", "Edit", "WebSearch", "WebFetch"],
                permission_mode="bypassPermissions",
                max_turns=4,
                output_format={"type": "json_schema", "schema": COMPANION_SCHEMA},
                setting_sources=[],
            )
            _session_client = ClaudeSDKClient(options=options)
            await _session_client.connect()
            _session_turns = 0

        t0 = _time.monotonic()

        def _meter(message=None, error: str | None = None):
            try:
                from . import cost
                cost.record_run(
                    "companion", model,
                    getattr(message, "total_cost_usd", 0.0) if message else 0.0,
                    getattr(message, "usage", None) if message else None,
                    duration_ms=int((_time.monotonic() - t0) * 1000),
                    ok=error is None, error=error)
            except Exception:
                pass

        try:
            async with asyncio.timeout(timeout):
                await _session_client.query(prompt)
                async for message in _session_client.receive_response():
                    if isinstance(message, ResultMessage):
                        _session_turns += 1
                        if message.subtype == "success":
                            if message.structured_output is None:
                                err = ("companion turn returned no structured output: "
                                       f"{(message.result or '')[:200]}")
                                _meter(message, error=err)
                                raise RuntimeError(err)
                            _meter(message)
                            return message.structured_output
                        _meter(message, error=f"companion turn failed: {message.subtype}")
                        raise RuntimeError(f"companion turn failed: {message.subtype}")
        except Exception:
            # the process may be wedged/desynced - drop it, next turn reconnects clean
            await _session_reset()
            raise
    raise RuntimeError("companion turn produced no result")


async def _ask(prompt: str, timeout: float) -> dict:
    """Route one conversational turn to whichever provider the user has picked
    (Providers card / picker in the CoS panel). Anthropic keeps the warm
    persistent session above; codex spawns per turn. Kept as one seam so the
    two chat entry points below stay identical regardless of provider.

    A provider failure is NOT silently retried on another provider: if the user
    picked codex and codex is broken, they need to see that, not get a Claude
    answer they didn't ask for and can't distinguish."""
    from . import providers
    provider, model = providers.selection()
    if provider == "codex":
        return await providers.ask_codex(prompt, model, COMPANION_SCHEMA, timeout)
    return await _companion_ask(prompt, model, timeout)


def _snapshot() -> str:
    """Compact live-state digest the companion consults from."""
    parts = []
    run = db.query("SELECT id, profile, status FROM swarm_runs ORDER BY id DESC LIMIT 1")
    running = db.query("SELECT COUNT(*) AS n FROM swarm_runs WHERE status='running'")[0]["n"]
    parts.append(
        (f"latest swarm run: #{run[0]['id']} ({run[0]['profile']}, {run[0]['status']})"
         if run else "no swarm runs yet") + f"; running now: {running}")
    wts = db.query("SELECT status, COUNT(*) AS n FROM swarm_worktrees GROUP BY status")
    parts.append("worktrees: " + (", ".join(f"{r['status']}:{r['n']}" for r in wts) or "none"))
    ws = db.query("SELECT id, name, enabled, last_note FROM watchers ORDER BY id")
    parts.append("watchers: " + ("; ".join(
        f"[{w['id']}] {w['name']}{'' if w['enabled'] else ' (OFF)'}"
        f" - {(w['last_note'] or 'no runs yet')[:60]}" for w in ws) or "none"))
    drafts = db.query("SELECT COUNT(*) AS n FROM drafts WHERE status='pending'")[0]["n"]
    parts.append(f"pending approval drafts: {drafts}")
    evs = db.query("SELECT detail FROM audit_log WHERE action='watcher_event' "
                   "ORDER BY id DESC LIMIT 4")
    if evs:
        parts.append("recent watcher events: " + " | ".join(
            json.loads(e["detail"]).get("headline", "")[:70] for e in evs))
    parts.append(f"dry_run: {db.get_setting('dry_run')}, "
                 f"kill_switch: {db.get_setting('kill_switch') or 'false'}")
    return "\n".join(parts)


async def _exec(cmd: dict) -> str:
    """One companion command through the app's real machinery. Audited."""
    from . import swarm, watchers, worktrees
    a, g = cmd.get("action"), cmd.get("args") or {}
    db.audit("companion_command", {"action": a, "args": g}, actor="user")
    try:
        if a == "swarm_run":
            rid = swarm.start_run(trigger="companion",
                                  item_ids=g.get("item_ids") or None,
                                  profile=str(g.get("profile") or "thread"))
            return f"swarm run #{rid} started ({g.get('profile') or 'thread'})"
        if a == "swarm_retry":
            r = await swarm.retry_stage(int(g["item_id"]), str(g.get("stage") or "scan"))
            return f"#{g['item_id']}: {r.get('note')}"
        if a == "swarm_control":
            run = db.query("SELECT id FROM swarm_runs WHERE status='running' "
                           "ORDER BY id DESC LIMIT 1")
            if not run:
                return "no running swarm to control"
            swarm.control(run[0]["id"], str(g.get("action") or "pause"), None)
            return f"run #{run[0]['id']}: {g.get('action')}"
        if a == "housekeeping":
            out = await swarm.housekeeping_sweep()
            keys = [k for k in out if k != "idle"]
            return "housekeeping ran: " + (", ".join(keys) if keys else "all quiet")
        if a == "watcher_run":
            r = await watchers.sweep(force_id=int(g["id"]))
            return "; ".join(f"{k}: {v}" for k, v in r.items())[:220]
        if a == "watcher_toggle":
            watchers.update_watcher(int(g["id"]), {"enabled": bool(g.get("enabled"))})
            return f"watcher {g['id']} {'watching' if g.get('enabled') else 'paused'}"
        if a == "do_work":
            task = str(g.get("task") or "").strip()
            if not task:
                return "do_work needs a task"
            repo = str(g.get("repo") or "").strip()
            if not repo:
                repos = list((policy().get("swarm", {}).get("code", {})
                              .get("project_repos") or {}).values())
                repo = repos[0] if repos else ""
            if not repo:
                return "no repo mapping configured (swarm.code.project_repos)"
            run_id = db.execute(
                "INSERT INTO swarm_runs(trigger, profile, started_at) VALUES(?,?,?)",
                ("companion", "thread", db.now()))

            async def _bg():
                try:
                    wt = await worktrees.create_worktree(
                        repo, f"companion-r{run_id}", run_id=run_id, job_id=None,
                        kind="ticket", title=task[:80],
                        target_ref=f"companion-r{run_id}")
                    res = None
                    if wt:
                        res = await swarm._code_one(run_id, "thread", wt, task,
                                                    policy().get("swarm", {}))
                    db.execute(
                        "UPDATE swarm_runs SET finished_at=?, status='done', totals=? "
                        "WHERE id=?",
                        (db.now(), json.dumps({"companion_task": task[:150],
                                               "worktrees": [res] if res else []}),
                         run_id))
                    # pillar 3: follow through - report back unprompted when the
                    # long-running work lands (proactive sweep surfaces this)
                    pr = (res or {}).get("pr") if isinstance(res, dict) else None
                    db.audit("companion_work_done", {
                        "task": task[:120], "run_id": run_id,
                        "ok": bool(res and res.get("ok")),
                        "pr": (pr or {}).get("url") if isinstance(pr, dict) else None})
                except Exception as e:
                    db.execute("UPDATE swarm_runs SET finished_at=?, status='error', "
                               "error=? WHERE id=?", (db.now(), str(e)[:200], run_id))
                    db.audit("companion_work_done", {"task": task[:120],
                             "run_id": run_id, "ok": False, "error": str(e)[:150]})

            asyncio.create_task(_bg())
            return (f"Claude coder session spawned in {repo} (run #{run_id}) - "
                    "it commits and ships a draft PR through the normal pipeline")
        if a == "kill_switch_on":
            db.set_setting("kill_switch", "true")
            return "KILL SWITCH ON - all outbound blocked; re-enable from the board"

        # ---- desktop agent ----------------------------------------------------
        from . import desktop
        if a == "desktop_status":
            return await desktop.status()
        if a == "run":
            r = await desktop.run_command(str(g.get("project") or ""),
                                          str(g.get("command") or ""))
            if r.get("needs_confirm"):
                return r["note"]
            return (f"{g.get('project')}: `{r['cmd']}` -> exit {r['exit']}\n"
                    f"{(r.get('tail') or '').strip()[-500:]}")
        if a == "confirm":
            p = desktop.take_pending(str(g.get("token") or ""))
            if not p:
                return "no pending command for that token (expired or wrong)"
            r = await desktop.run_command(p["project"], p["cmd"], confirmed=True)
            return (f"CONFIRMED {p['project']}: `{r['cmd']}` -> exit {r['exit']}\n"
                    f"{(r.get('tail') or '').strip()[-500:]}")
        if a == "open_terminal":
            r = await desktop.open_terminal(str(g.get("project") or ""),
                                            str(g.get("command") or ""))
            if r.get("needs_confirm"):
                return r["note"]
            return f"{r['session']} started in {r['project']}: `{r['cmd']}` (pid {r['pid']})"
        if a == "terminal_list":
            ts = desktop.list_terminals()
            def _state(t):
                return "running" if t["running"] else f"exit {t.get('exit')}"
            return "; ".join(f"{t['id']} [{_state(t)}] {t['cmd'][:40]}"
                             for t in ts) or "no terminal sessions"
        if a == "terminal_output":
            r = desktop.terminal_output(str(g.get("id") or ""))
            if r.get("error"):
                return r["error"]
            state = "running" if r["running"] else f"exit {r.get('exit')}"
            return f"{r['id']} [{state}]\n{r['tail']}"
        if a == "terminal_send":
            r = await desktop.terminal_send(str(g.get("id") or ""),
                                            str(g.get("text") or ""))
            return r.get("error") or r.get("note") or f"sent to {r['id']}"
        if a == "terminal_stop":
            r = await desktop.terminal_stop(str(g.get("id") or ""))
            return r.get("error") or f"{r['id']} stopped"
        if a == "vscode":
            r = await desktop.vscode_open(str(g.get("project") or ""),
                                          str(g.get("file") or ""),
                                          int(g.get("line") or 0))
            return r.get("error") or f"opened in VS Code: {r['opened']}"
        if a == "files_ls":
            r = desktop.files_ls(str(g.get("project") or ""),
                                 str(g.get("path") or ""),
                                 int(g.get("depth") or 2))
            return f"{r['path']}:\n" + "\n".join(r["entries"][:40])
        if a == "file_read":
            r = desktop.file_read(str(g.get("project") or ""),
                                  str(g.get("path") or ""))
            if r.get("error"):
                return r["error"]
            trunc = " (truncated)" if r.get("truncated") else ""
            return f"{r['path']}{trunc}:\n{r['content'][:1200]}"
        if a == "search_code":
            r = await desktop.search_code(str(g.get("project") or ""),
                                          str(g.get("query") or ""))
            return f"search '{r['query']}':\n{(r.get('hits') or '').strip() or 'no hits'}"
        if a == "changed_files":
            r = await desktop.changed_files(str(g.get("project") or ""))
            return f"{r['project']} changes:\n{r['changes']}"

        # ---- calendar + email (pillar 6: one conversation, whole world) ------
        if a == "calendar":
            from datetime import date, timedelta
            when = str(g.get("when") or "today").lower()
            day = date.today() + (timedelta(days=1) if "tomorrow" in when else timedelta())
            evs = await graph.calendar_events(day)
            if not evs:
                return f"nothing on your calendar {when}."
            lines = []
            for e in evs[:8]:
                t = (e.get("start_local") or "")[11:16]
                lines.append(f"{t or 'all day'} - {e.get('subject', '(no title)')}")
            return f"your {when}:\n" + "\n".join(lines)
        if a == "email":
            mails = await graph.list_emails(top=int(g.get("top") or 6))
            if not mails:
                return "inbox looks clear."
            lines = []
            for m in mails[:6]:
                frm = (m.get("from") or m.get("sender") or {})
                name = (frm.get("emailAddress", {}).get("name")
                        if isinstance(frm, dict) else "") or "?"
                lines.append(f"- {name}: {(m.get('subject') or '(no subject)')[:70]}")
            return "recent email:\n" + "\n".join(lines)

        if a == "docs":
            # pillar 6: Obsidian vault + Confluence, one query
            q = str(g.get("query") or "").strip()
            from . import obsidian
            hits = obsidian.search_vault(q, limit=5) if q else obsidian.recent_notes(5)
            lines = [f"- {h.get('name') or h.get('path', '?')}" for h in hits]
            try:
                from . import confluence
                cbase, cauth = confluence._base_auth()
                cpages = confluence._search(
                    cbase, cauth, f'text ~ "{q}"' if q else "type=page order by lastmodified desc", 4)
                lines += [f"- (confluence) {p.get('title', '?')}" for p in cpages]
            except Exception:
                pass
            return (f"docs for '{q}':\n" if q else "recent notes:\n") + (
                "\n".join(lines[:9]) or "nothing found")

        # ---- memory (pillar 1) ------------------------------------------------
        from . import companion_memory as mem
        if a == "mem_add":
            mem.remember_from_text(str(g.get("text") or ""))
            return "got it, i'll remember that."
        if a == "memory":  # model-emitted: persist something it learned
            mem.add(str(g.get("text") or ""), kind=str(g.get("kind") or "fact"),
                    topic=str(g.get("topic") or ""), source="learned",
                    pinned=bool(g.get("pinned")))
            return "noted"
        if a == "mem_forget":
            n = mem.forget(str(g.get("query") or ""))
            return f"forgot {n} thing{'s' if n != 1 else ''}." if n else "nothing matched that."
        if a == "mem_recall":
            hits = mem.recall(str(g.get("query") or ""), limit=10)
            if not hits:
                return "nothing on that yet."
            return "here's what i've got:\n" + "\n".join(f"- {h['content']}" for h in hits)
        return f"unknown command '{a}'"
    except Exception as e:
        log.warning("companion command %s failed: %s", a, e)
        return f"{a} failed: {str(e)[:140]}"


# Explicit command syntax → skip the LLM entirely (no ~12s subprocess spawn).
# This is what makes typed commands feel instant; natural language still falls
# through to the model for a real conversation.
def _fast_command(text: str) -> list[dict] | None:
    from . import companion_memory as mem
    t = text.strip()
    low = t.lower()

    # memory directives are instant + deterministic (no model)
    d = mem.parse_directive(t)
    if d:
        op, payload = d
        if op == "remember":
            return [{"action": "mem_add", "args": {"text": payload}}]
        if op == "forget":
            return [{"action": "mem_forget", "args": {"query": payload}}]
        if op == "recall":
            return [{"action": "mem_recall", "args": {"query": payload}}]

    def m(pat):
        return re.match(pat, t, re.I)

    if low in ("status", "desktop status", "desktop", "ds"):
        return [{"action": "desktop_status", "args": {}}]
    if low in ("projects", "list projects", "repos", "repositories"):
        return [{"action": "desktop_status", "args": {}}]
    if low in ("terminals", "terminal sessions", "sessions"):
        return [{"action": "terminal_list", "args": {}}]
    if low in ("housekeeping", "sync", "housekeep"):
        return [{"action": "housekeeping", "args": {}}]
    if low in ("calendar", "my calendar", "what's on my calendar", "whats on my calendar",
               "schedule", "my day", "agenda"):
        return [{"action": "calendar", "args": {"when": "today"}}]
    if low in ("calendar tomorrow", "tomorrow", "what's on tomorrow", "whats on tomorrow"):
        return [{"action": "calendar", "args": {"when": "tomorrow"}}]
    if low in ("email", "inbox", "my email", "any email", "recent email", "mail"):
        return [{"action": "email", "args": {}}]
    if low in ("docs", "notes", "recent notes", "recent docs"):
        return [{"action": "docs", "args": {}}]
    g = m(r"^(?:search\s+)?(?:docs|notes)\s+(?:for\s+)?(.+)$")
    if g:
        return [{"action": "docs", "args": {"query": g.group(1).strip()}}]
    if low in ("kill switch on", "kill switch", "emergency stop", "stop everything"):
        return [{"action": "kill_switch_on", "args": {}}]
    g = m(r"^confirm\s+([a-f0-9]{4,8})$")
    if g:
        return [{"action": "confirm", "args": {"token": g.group(1)}}]
    g = m(r"^run\s+(.+?)\s+in\s+(\S.*)$")
    if g:
        return [{"action": "run", "args": {"command": g.group(1).strip(),
                                           "project": g.group(2).strip()}}]
    g = m(r"^(?:git status|changed files|changes)\s+(?:in\s+)?(\S.*)$")
    if g:
        return [{"action": "changed_files", "args": {"project": g.group(1).strip()}}]
    g = m(r"^search\s+(?:code\s+)?(.+?)\s+in\s+(\S.*)$")
    if g:
        return [{"action": "search_code", "args": {"query": g.group(1).strip(),
                                                    "project": g.group(2).strip()}}]
    g = m(r"^read\s+(\S+)\s+in\s+(\S.*)$")
    if g:
        return [{"action": "file_read", "args": {"path": g.group(1).strip(),
                                                 "project": g.group(2).strip()}}]
    g = m(r"^(?:files|ls)\s+(?:in\s+)?(\S.*)$")
    if g:
        return [{"action": "files_ls", "args": {"project": g.group(1).strip()}}]
    g = m(r"^open\s+(?:vscode|vs code|code)\s+(\S+)(?::(\d+))?\s*(?:in\s+(\S.*))?$")
    if g:
        # "open code <project>" or "open code <file>:<line> in <project>"
        if g.group(3):
            return [{"action": "vscode", "args": {"project": g.group(3).strip(),
                                                  "file": g.group(1),
                                                  "line": int(g.group(2) or 0)}}]
        return [{"action": "vscode", "args": {"project": g.group(1)}}]
    g = m(r"^terminal\s+(\S+)\s+output$") or m(r"^(term-\d+)\s+output$")
    if g:
        return [{"action": "terminal_output", "args": {"id": g.group(1)}}]
    g = m(r"^terminal\s+(\S+)\s+stop$") or m(r"^stop\s+(term-\d+)$")
    if g:
        return [{"action": "terminal_stop", "args": {"id": g.group(1)}}]
    g = m(r"^(?:terminal\s+(\S+)\s+send|send to (\S+))\s+(.+)$")
    if g:
        sid = g.group(1) or g.group(2)
        return [{"action": "terminal_send", "args": {"id": sid, "text": g.group(3)}}]
    return None


async def sweep() -> dict:
    """Read the companion chat; answer + execute the user's newest messages."""
    from . import actions
    cfg = policy().get("companion", {}) or {}
    if not cfg.get("enabled", True):
        return {"skipped": "companion.enabled is false"}
    chat_id = cfg.get("chat_id") or policy().get("me", {}).get("self_chat_id")
    if not chat_id:
        return {"skipped": "no companion chat configured"}
    try:
        msgs = await graph.read_chat_messages(chat_id, top=15)
    except Exception as e:
        log.warning("companion read failed: %s", e)
        return {"error": str(e)[:120]}
    cursor = db.get_setting("companion_cursor")
    me_id = (policy().get("me", {}).get("aad_id") or "").lower()
    history, fresh, newest_any = [], [], ""
    for m in reversed(msgs):  # oldest first
        body = re.sub(r"<[^>]+>", " ",
                      str((m.get("body") or {}).get("content", "") or "")).strip()
        if not body:
            continue
        at = (m.get("createdDateTime") or "").replace("Z", "+00:00")
        newest_any = max(newest_any, at)
        is_cos = body.startswith("🤖")
        # never feed the model its own leaked/meta replies - that is the loop
        # that made it repeat "StructuredOutput format" every turn
        if not (is_cos and _LEAK.search(body)):
            history.append(f"[{'CoS' if is_cos else 'User'} {at[:16]}] {body[:500]}")
        # identity gate: only messages AUTHORED by the user's aad_id count as
        # directives (a forwarded/copied message from elsewhere never executes)
        sender = (((m.get("from") or {}).get("user") or {}).get("id") or "").lower()
        if not is_cos and sender == me_id and (not cursor or at > cursor):
            fresh.append({"at": at, "text": body[:800]})
    if cursor is None:
        # first ever pass: baseline, never answer historical notes
        db.set_setting("companion_cursor", newest_any or db.now())
        return {"baselined": len(msgs)}
    if not fresh:
        return {"idle": True}
    newest = fresh[-1]["at"]

    # FAST PATH: a single explicit command skips the LLM (sub-second vs ~12s).
    fast = _fast_command(fresh[-1]["text"]) if len(fresh) == 1 else None
    if fast:
        db.set_setting("companion_cursor", newest)  # before exec (crash-safe)
        outcomes = [await _exec(cmd) for cmd in fast]
        try:
            await actions.companion_reply("🤖 " + "\n".join(outcomes))
        except actions.SendBlocked:
            pass
        log.info("companion FAST: %s -> %d command(s)", fresh[-1]["text"][:50], len(fast))
        return {"messages": 1, "fast": True, "commands": outcomes}

    from . import companion_memory as mem
    joined = " ".join(f["text"] for f in fresh)
    memblock = mem.context(joined)
    _mn, steer = _mood(fresh[-1]["text"])
    verdict = await _ask(
        (f"# Live board snapshot\n{_snapshot()}\n\n"
         + (f"# What you remember (use it naturally, don't recite it)\n{memblock}\n\n"
            if memblock else "")
         + (f"# Read the room\n{steer}\n\n" if steer else "")
         + f"# Recent conversation\n" + "\n".join(history[-12:]) + "\n\n"
         f"# The user's newest message(s) — answer these\n"
         + "\n".join(f"- {f['text']}" for f in fresh)),
        # Explicit commands never reach here (fast-path handles them with no
        # model), so this path is pure conversation/consult. Provider and model
        # come from the user's picker (providers.selection()); the Anthropic path
        # keeps the persistent session so the back-and-forth stays quick.
        timeout=90)
    reply = (verdict.get("reply") or "").strip()
    cmds = verdict.get("commands") or []
    # reject meta-leak / filler so it never posts (and never re-enters history)
    if _bad_reply(reply):
        log.warning("companion: rejected low-value reply %r", reply[:80])
        reply = "" if cmds else "sorry, glitched for a sec there - what's up?"
    posted = "suppressed"
    if reply:
        try:
            posted = await actions.companion_reply(reply)
        except actions.SendBlocked as e:
            posted = f"blocked: {e}"
    # cursor BEFORE executing commands: a crash mid-command must not make the
    # sweep re-answer (and re-execute) the same message forever
    db.set_setting("companion_cursor", newest)
    outcomes = []
    for cmd in cmds[:3]:
        outcomes.append(await _exec(cmd))
    if outcomes:
        try:
            await actions.companion_reply("🤖 ↳ " + " · ".join(outcomes))
        except actions.SendBlocked:
            pass
    log.info("companion: %d message(s) -> reply %s, %d command(s)",
             len(fresh), posted, len(outcomes))
    return {"messages": len(fresh), "reply": posted, "commands": outcomes}


async def answer(text: str, history: list[str] | None = None) -> dict:
    """Synchronous single-turn: used by the in-app companion panel over local
    HTTP (no Teams, no polling, no Graph) so commands come back in well under
    a second. Same fast-path + command engine as the Teams sweep. Returns
    {reply, outcomes, fast, ms}."""
    import time as _t
    t0 = _t.monotonic()
    fast = _fast_command(text.strip())
    if fast:
        outcomes = [await _exec(cmd) for cmd in fast]
        return {"reply": "\n".join(outcomes), "outcomes": outcomes,
                "fast": True, "ms": int((_t.monotonic() - t0) * 1000)}
    from . import companion_memory as mem
    memblock = mem.context(text)
    mood, steer = _mood(text)
    verdict = await _ask(
        (f"# Live board snapshot\n{_snapshot()}\n\n"
         + (f"# What you remember (use it naturally, don't recite it)\n{memblock}\n\n"
            if memblock else "")
         + (f"# Read the room\n{steer}\n\n" if steer else "")
         + (f"# Recent conversation\n{chr(10).join(history[-8:])}\n\n" if history else "")
         + f"# The user's message - answer this\n{text}"),
        timeout=90)
    reply = (verdict.get("reply") or "").strip()
    cmds = verdict.get("commands") or []
    if _bad_reply(reply):
        reply = "" if cmds else "sorry, brain glitched for a sec - say that again?"
    outcomes = [await _exec(c) for c in cmds[:3]]
    return {"reply": reply, "outcomes": outcomes, "fast": False,
            "ms": int((_t.monotonic() - t0) * 1000)}


PROACTIVE = AgentDef(
    name="companion_proactive",
    system_prompt=(
        "You are CoS, the user's chief of staff, pinging THEM unprompted in their "
        "private chat because something moved that they'd want to know. You get a "
        "list of what just changed and the live board. Write ONE short, warm, "
        "friend-toned heads-up (a couple sentences, no bullet dumps unless it's "
        "a morning brief) that leads with what matters most and why it matters "
        "to them now. Be specific - real ids, names, states. If nothing here is "
        "actually worth interrupting them for, say so with an empty reply. Never "
        "mention tools, schemas, or internal plumbing. Hyphens, not em dashes."
    ),
)
_PROACTIVE_SCHEMA = {
    "type": "object",
    "properties": {"nudge": {"type": "string",
                             "description": "the message to the user, or empty to stay quiet"}},
    "required": ["nudge"],
}

# audit actions worth proactively surfacing, with a human label
_MATERIAL = {
    "watcher_event": "channel",
    "swarm_ado_state": "ticket state",
    "swarm_pr_chat": "PR ready",
    "swarm_clarify_resume": "unblocked",
    "swarm_run": "swarm run",
    "companion_work_done": "your task",
}


async def proactive_sweep() -> dict:
    """Pillar 2: CoS initiates. Batches material changes since the last check
    into one in-voice heads-up (deduped by an audit-id cursor so it never
    pings twice for the same thing), plus a once-a-day morning brief. Honors
    the kill switch and a min gap so it never spams."""
    from . import actions
    from datetime import datetime
    cfg = policy().get("companion", {}) or {}
    if not cfg.get("enabled", True) or not cfg.get("proactive", True):
        return {"skipped": "proactive off"}
    if db.get_setting("kill_switch") == "true":
        return {"skipped": "kill switch"}

    raw = db.get_setting("companion_proactive_cursor")
    maxid = db.query("SELECT COALESCE(MAX(id),0) AS m FROM audit_log")[0]["m"]
    if raw is None:  # never baselined (distinct from a real cursor of 0)
        db.set_setting("companion_proactive_cursor", str(maxid))
        return {"baselined": maxid}
    cursor = int(raw)

    ph = ",".join("?" * len(_MATERIAL))
    rows = db.query(
        f"SELECT id, action, detail FROM audit_log WHERE id>? AND action IN ({ph}) "
        f"ORDER BY id", (cursor, *_MATERIAL))
    changes = []
    for r in rows:
        try:
            d = json.loads(r["detail"])
        except Exception:
            d = {}
        if d.get("dry_run"):
            continue
        if r["action"] == "watcher_event" and d.get("severity") == "info":
            continue  # only nudge on attention/urgent channel events
        changes.append(_change_line(r["action"], d))
    changes = [c for c in changes if c]

    # morning brief once per weekday, in the configured window
    now = datetime.now()
    brief_hour = int(cfg.get("brief_hour", 9))
    is_brief = (now.hour == brief_hour and now.weekday() < 5
                and db.get_setting("companion_brief_day") != now.date().isoformat())

    if not changes and not is_brief:
        db.set_setting("companion_proactive_cursor", str(maxid))
        return {"idle": True}

    header = ("MORNING BRIEF - it's the start of the user's day, give them the "
              "ranked rundown of what needs them.\n\n" if is_brief else
              "These just changed - decide if any of it is worth a heads-up.\n\n")
    verdict = await run_agent(
        PROACTIVE,
        f"# {header}# What changed\n" + ("\n".join(f"- {c}" for c in changes)
                                         or "- (nothing new since last check)")
        + f"\n\n# Live board\n{_snapshot()}",
        schema=_PROACTIVE_SCHEMA, with_tools=False, max_turns=4,
        model=cfg.get("model") or "claude-sonnet-4-6",
        label="companion:proactive", timeout=90)
    nudge = (verdict.get("nudge") or "").strip()
    db.set_setting("companion_proactive_cursor", str(maxid))
    if is_brief:
        db.set_setting("companion_brief_day", now.date().isoformat())
    if not nudge or _bad_reply(nudge):
        return {"changes": len(changes), "nudge": "stayed quiet"}
    try:
        posted = await actions.companion_reply(nudge)
    except actions.SendBlocked as e:
        posted = f"blocked: {e}"
    db.audit("companion_proactive", {"changes": len(changes), "brief": is_brief,
                                     "posted": posted})
    log.info("companion proactive: %d change(s), brief=%s -> %s",
             len(changes), is_brief, posted)
    return {"changes": len(changes), "brief": is_brief, "posted": posted}


def _change_line(action: str, d: dict) -> str:
    if action == "watcher_event":
        return f"[{d.get('severity', 'info')}] {d.get('watcher', 'watcher')}: {d.get('headline', '')}"
    if action == "swarm_ado_state":
        return f"ticket AB#{d.get('ado_item')} moved to {d.get('state')}"
    if action == "swarm_pr_chat" and not d.get("dry_run"):
        return f"a swarm PR was announced ready for review: {d.get('pr_url', '')}"
    if action == "swarm_clarify_resume":
        return f"AB#{d.get('ado_item')} got an answer and work resumed"
    if action == "swarm_run":
        return (f"swarm run #{d.get('run_id')} ({d.get('profile', '?')}) finished: "
                f"{d.get('pass', 0)} pass, {d.get('fail', 0)} fail, {d.get('blocked', 0)} blocked")
    if action == "companion_work_done":
        if d.get("ok"):
            return (f"the task you asked for finished - '{d.get('task', '')}'"
                    + (f", PR: {d['pr']}" if d.get("pr") else ", committed"))
        return f"the task you asked for hit a snag - '{d.get('task', '')}': {d.get('error', 'see the board')}"
    return ""


async def run_loop() -> None:
    """Dedicated adaptive poll loop (started at app boot) so the companion
    feels instant while the user is chatting, without hammering Graph when they
    are not. Right after any activity it polls every `hot_seconds`; after a quiet
    stretch it backs off to `warm_seconds`, then `idle_seconds`. A single read
    of one chat is cheap, so the hot window can be tight."""
    from . import companion_memory as mem
    mem.seed_defaults()
    cfg = policy().get("companion", {}) or {}
    if not cfg.get("enabled", True):
        return
    hot = float(cfg.get("hot_seconds", 0.7))
    warm = float(cfg.get("warm_seconds", 8))
    idle = float(cfg.get("idle_seconds", 45))
    hot_ticks = int(cfg.get("hot_window_ticks", 90))
    warm_ticks = hot_ticks + int(cfg.get("warm_window_ticks", 40))
    quiet = 0
    log.info("companion loop started (hot=%ss warm=%ss idle=%ss)", hot, warm, idle)
    while True:
        try:
            res = await sweep()
            if res.get("messages"):
                quiet = 0
                # rapid follow-up burst: after handling a message, immediately
                # re-poll a few times with almost no gap (people fire several
                # short messages in a row) before settling into hot cadence
                for _ in range(4):
                    await asyncio.sleep(0.15)
                    if not (await sweep()).get("messages"):
                        break
            else:
                quiet += 1
        except Exception:
            log.exception("companion loop tick failed (continuing)")
            quiet += 1
        delay = hot if quiet < hot_ticks else warm if quiet < warm_ticks else idle
        await asyncio.sleep(delay)
