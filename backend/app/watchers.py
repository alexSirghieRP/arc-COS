"""Watchers: configurable monitors over channels and processes (owner decision,
2026-07-10). Each watcher has a target, an on/off toggle, and GUARDRAILS —
free-text rules, visible and editable from the board — that tell the watcher
what to look for, what to ignore, and what it may never do.

Kinds:
- teams_chat: reads new messages in one chat each pass; a small read-only
  agent applies the guardrails and emits events (info/attention/urgent).
  Watchers never write to the channel — they only observe and surface.
- process:   checks one scheduler job in the health tracker; emits an event
  when the job is failing (error streak) or has gone silent.

Events land in the audit trail (action=watcher_event) and render in the
Watchers panel. Sweep runs every couple of minutes; each watcher honors its
own interval.
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timezone

from . import db, graph
from .agents import AgentDef, run_agent
from .config import policy

log = logging.getLogger("chief.watchers")

WATCHER_AGENT = AgentDef(
    name="watcher",
    system_prompt=(
        "You are a channel watcher for the owner's chief-of-staff system. You get the "
        "watcher's GUARDRAILS (the owner's rules for what matters here) and a batch of "
        "new messages. Apply the guardrails strictly: emit an event ONLY for "
        "messages the guardrails care about, ignore everything they exclude. "
        "Judge only from the provided messages; never invent facts. Severity: "
        "info = good to know, attention = the owner should look today, urgent = now. "
        "You cannot look things up yourself — when an event needs verification "
        "against ADO or GitHub (a ticket's real state, a PR's checks/reviews), "
        "set its 'check' field and an ephemeral worker with fresh context will "
        "investigate and attach its finding. No em dashes."
    ),
)

WATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "headline": {"type": "string", "description": "one line, cite who/what"},
                    "severity": {"type": "string", "enum": ["info", "attention", "urgent"]},
                    "why": {"type": "string", "description": "which guardrail this matches"},
                    "check": {
                        "type": "object",
                        "description": "OPTIONAL follow-up an ephemeral worker should run",
                        "properties": {
                            "kind": {"type": "string", "enum": ["ado_item", "github_pr", "none"]},
                            "ref": {"type": "string",
                                    "description": "work item id (e.g. 12345) or PR ref (#732 / url)"},
                        },
                        "required": ["kind", "ref"],
                    },
                    "needs_alex": {"type": "boolean",
                                   "description": "the owner personally must address this today"},
                    "suggested_reply": {"type": "string",
                                        "description": "OPTIONAL short reply the owner could send, "
                                                       "first person, conversational"},
                },
                "required": ["headline", "severity", "why"],
            },
        },
    },
    "required": ["events"],
}

CHECK_AGENT = AgentDef(
    name="watcher_check",
    system_prompt=(
        "You are an ephemeral checker spawned by a watcher (fresh context, one "
        "shot). You get a watcher event and the CURRENT data of the ADO item or "
        "GitHub PR it references. Verify the event against the data and return "
        "one factual finding line: confirm, correct, or add the current state "
        "(states, checks, reviews, dates). Judge only from the data. No em dashes."
    ),
)

CHECK_SCHEMA = {
    "type": "object",
    "properties": {"finding": {"type": "string", "description": "one line, <=40 words"}},
    "required": ["finding"],
}

DEFAULT_GUARDRAILS = {
    "teams_channel": (
        "Watch for: requested changes or review feedback on PRs, failures "
        "(CI, deploy, UAT), questions addressed to the owner, and decisions that "
        "affect their tickets.\n"
        "Ignore: the swarm's own \U0001F916 posts, social chatter, and routine "
        "status posts with no action for the owner.\n"
        "Never write to the channel. At most 3 events per pass."
    ),
    "teams_chat": (
        "Watch for: requested changes or review feedback on PRs, failures "
        "(CI, deploy, UAT), questions addressed to the owner, and decisions that "
        "affect their tickets.\n"
        "Ignore: the swarm's own \U0001F916 posts, social chatter, and routine "
        "status posts with no action for the owner.\n"
        "Never write to the channel. At most 3 events per pass."
    ),
    "process": (
        "Emit 'attention' when the job is failing (error streak >= 2) and "
        "'urgent' when it has been failing for over an hour. Ignore single "
        "transient failures."
    ),
}


def seed_defaults() -> None:
    """First boot: create the PR-review-chat watcher the owner asked for."""
    if db.query("SELECT 1 FROM watchers LIMIT 1"):
        return
    pr_chat = (policy().get("swarm", {}).get("receipts", {}) or {}).get("pr_chat_id")
    if pr_chat:
        db.execute(
            "INSERT INTO watchers(name, kind, target, enabled, guardrails, "
            "interval_minutes, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            ("PR review chat", "teams_chat", pr_chat, 1,
             DEFAULT_GUARDRAILS["teams_chat"], 5, db.now(), db.now()))
        log.info("watchers: seeded default PR-review-chat watcher")


async def _chat_topics() -> dict[str, str]:
    """chat id -> Teams display name (topic), best effort from the cache."""
    try:
        return {c["id"]: (c.get("topic") or "").strip()
                for c in await graph.chats_cached()}
    except Exception:
        return {}


async def list_watchers() -> list[dict]:
    rows = [dict(r) for r in db.query(
        "SELECT * FROM watchers ORDER BY enabled DESC, id")]
    topics = await _chat_topics() if any(
        w["kind"] == "teams_chat" for w in rows) else {}
    for w in rows:
        if w["kind"] == "teams_chat":
            # the chat's name AS IN TEAMS (the owner's rule); raw id is a tooltip
            w["target_label"] = topics.get(w["target"]) or None
        w["events"] = [
            {"ts": e["ts"], **json.loads(e["detail"])}
            for e in db.query(
                "SELECT ts, detail FROM audit_log WHERE action='watcher_event' "
                "AND json_extract(detail,'$.watcher_id')=? ORDER BY id DESC LIMIT 8",
                (w["id"],))]
    return rows


def _extract_chat_id(target: str) -> str:
    """Accept a raw chat id OR a pasted teams.microsoft.com link."""
    import urllib.parse
    m = re.search(r"(19:[^/\s]+@thread\.v2)", urllib.parse.unquote(target))
    return m.group(1) if m else target.strip()


def _parse_channel_link(target: str) -> tuple[str, str, str] | None:
    """Team CHANNELS (19:...@thread.tacv2 + groupId) from a pasted link.
    Returns (team_id, channel_id, channel_name) or None."""
    import urllib.parse
    t = urllib.parse.unquote(target)
    ch = re.search(r"(19:[^/\s]+@thread\.tacv2)", t)
    grp = re.search(r"[?&]groupId=([0-9a-fA-F-]{36})", t)
    if not ch:
        return None
    name = ""
    nm = re.search(r"@thread\.tacv2/([^?]+)", t)
    if nm:
        name = urllib.parse.unquote(nm.group(1)).strip("/ ")
    if grp:
        return grp.group(1), ch.group(1), name
    if "|" in target and not t.startswith("http"):  # raw "teamId|channelId"
        team, chan = target.split("|", 1)
        return team.strip(), chan.strip(), name
    return None


async def create_watcher(name: str, kind: str, target: str,
                         guardrails: str | None = None,
                         interval_minutes: int = 5) -> int:
    if kind not in ("teams_chat", "teams_channel", "process"):
        raise ValueError(f"unknown watcher kind {kind}")
    name = (name or "").strip()
    # a pasted CHANNEL link is recognized no matter which kind was selected
    chan = _parse_channel_link(target) if kind in ("teams_chat", "teams_channel") else None
    if chan:
        kind = "teams_channel"
        target = f"{chan[0]}|{chan[1]}"
        name = name or chan[2] or "Teams channel watcher"
    elif kind == "teams_channel":
        raise ValueError("paste the channel's Teams link (needs groupId + 19:...@thread.tacv2)")
    elif kind == "teams_chat":
        target = _extract_chat_id(target)
        if name in ("", "watcher"):
            name = (await _chat_topics()).get(target) or "Teams chat watcher"
    else:
        target = target.strip()
    return db.execute(
        "INSERT INTO watchers(name, kind, target, enabled, guardrails, "
        "interval_minutes, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (name or "watcher", kind, target, 1,
         (guardrails or DEFAULT_GUARDRAILS.get(kind, "")).strip(),
         max(1, int(interval_minutes)), db.now(), db.now()))


def update_watcher(wid: int, fields: dict) -> None:
    allowed = {"name", "enabled", "guardrails", "interval_minutes", "target", "prompt"}
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "enabled":
            v = 1 if v else 0
        if k == "interval_minutes":
            v = max(1, int(v))
        if k == "prompt":
            v = (v or "").strip() or None  # cleared prompt = back to the default
        sets.append(f"{k}=?")
        vals.append(v)
    if not sets:
        return
    vals += [db.now(), wid]
    db.execute(f"UPDATE watchers SET {', '.join(sets)}, updated_at=? WHERE id=?", vals)
    db.audit("watcher_update", {"watcher_id": wid, **fields}, actor="user")


def delete_watcher(wid: int) -> None:
    db.execute("DELETE FROM watchers WHERE id=?", (wid,))
    db.audit("watcher_delete", {"watcher_id": wid}, actor="user")


async def sweep(force_id: int | None = None) -> dict:
    """Run every due watcher (or one specific watcher, regardless of due)."""
    seed_defaults()
    out: dict = {}
    rows = db.query("SELECT * FROM watchers WHERE enabled=1") if force_id is None \
        else db.query("SELECT * FROM watchers WHERE id=?", (force_id,))
    now = datetime.now(timezone.utc)
    for w in rows:
        if force_id is None and w["last_run"]:
            try:
                age_min = (now - datetime.fromisoformat(w["last_run"])).total_seconds() / 60
                if age_min < w["interval_minutes"]:
                    continue
            except ValueError:
                pass
        try:
            wd = dict(w)
            if force_id is not None and wd["kind"] in ("teams_chat", "teams_channel"):
                # manual "Run now" = a fresh look at TODAY: rewind the cursor
                # to midnight so today's messages get (re)analyzed for things
                # the owner needs to address
                wd["cursor"] = now.strftime("%Y-%m-%dT00:00:00+00:00")
            if wd["kind"] in ("teams_chat", "teams_channel"):
                out[w["name"]] = await _watch_chat(wd)
            elif wd["kind"] == "process":
                out[w["name"]] = _watch_process(wd)
        except Exception as e:
            log.warning("watcher '%s' failed: %s", w["name"], e)
            db.execute("UPDATE watchers SET last_run=?, last_note=?, updated_at=? "
                       "WHERE id=?", (db.now(), f"error: {str(e)[:120]}", db.now(), w["id"]))
            out[w["name"]] = f"error: {str(e)[:80]}"
    return out or {"idle": True}


def _emit_events(w: dict, events: list[dict]) -> None:
    for ev in events[:5]:
        db.audit("watcher_event", {
            "watcher_id": w["id"], "watcher": w["name"],
            "headline": str(ev.get("headline", ""))[:200],
            "severity": ev.get("severity", "info"),
            "why": str(ev.get("why", ""))[:160],
            "checked": (str(ev["checked"])[:220] if ev.get("checked") else None),
            "needs_alex": bool(ev.get("needs_alex")),
            "draft_id": ev.get("draft_id"),
        }, actor="watcher")


async def _spawn_check(w: dict, ev: dict) -> str | None:
    """Cattle, not context (the owner's rule): whenever a watcher needs to look at
    ADO or GitHub, an EPHEMERAL one-shot worker does it — the watcher itself
    never ingests that data, so its context stays lean."""
    from . import ado
    chk = ev.get("check") or {}
    kind, ref = chk.get("kind"), str(chk.get("ref") or "")
    try:
        if kind == "ado_item":
            item_id = int(re.sub(r"[^0-9]", "", ref)[:8] or 0)
            if not item_id:
                return None
            data = await asyncio.to_thread(ado.item_details, item_id)
            what = f"ADO work item #{item_id}"
            evidence = json.dumps(data, ensure_ascii=False, default=str)[:14000]
        elif kind == "github_pr":
            pr = ref if "/" in ref else ref.lstrip("#")
            if "://" not in pr and "/" not in pr:
                # bare number: default to the primary swarm repo
                repos = list((policy().get("swarm", {}).get("code", {})
                              .get("project_repos") or {}).values())
                if not repos:
                    return None
                pr = f"https://github.com/{repos[0]}/pull/{pr}"
            proc = await asyncio.create_subprocess_exec(
                "gh", "pr", "view", pr, "--json",
                "title,state,reviewDecision,mergedAt,statusCheckRollup,comments",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            o, _ = await proc.communicate()
            what = f"GitHub PR {pr}"
            evidence = (o.decode() or "{}")[:14000]
        else:
            return None
        verdict = await run_agent(
            CHECK_AGENT,
            (f"# Watcher event to verify\n{ev.get('headline')}\n\n"
             f"# Current {what} data\n{evidence}"),
            schema=CHECK_SCHEMA, with_tools=False, max_turns=4,
            label=f"watcher:{w['id']}:check", timeout=90)
        return (verdict.get("finding") or "").strip() or None
    except Exception as e:
        log.warning("watcher check failed (%s %s): %s", kind, ref, e)
        return None


async def _watch_chat(w: dict) -> str:
    from . import actions
    if w["kind"] == "teams_channel":
        team_id, channel_id = w["target"].split("|", 1)
        if not actions.read_allowed(actions.channel_key(team_id, channel_id)):
            return "reading disabled (Write Access board)"
        msgs = await graph.read_channel_messages(team_id, channel_id, top=25)
    else:
        if not actions.read_allowed(w["target"]):
            return "reading disabled (Write Access board)"
        msgs = await graph.read_chat_messages(w["target"], top=25)
    fresh = []
    for m in msgs:
        body = re.sub(r"<[^>]+>", " ",
                      str((m.get("body") or {}).get("content", "") or "")).strip()
        if not body or body.startswith("\U0001F916"):
            continue
        at = (m.get("createdDateTime") or "").replace("Z", "+00:00")
        if w["cursor"] and at <= w["cursor"]:
            continue
        who = ((m.get("from") or {}).get("user") or {}).get("displayName") or "?"
        fresh.append({"who": who, "at": at, "text": body[:700]})
    if not fresh:
        db.execute("UPDATE watchers SET last_run=?, last_note='quiet — nothing new', "
                   "updated_at=? WHERE id=?", (db.now(), db.now(), w["id"]))
        return "quiet"
    fresh.sort(key=lambda x: x["at"])
    # the watcher's PROMPT (how it thinks) is tweakable per watcher from the
    # board; guardrails (what it cares about) ride along in the user message
    agent = WATCHER_AGENT if not w.get("prompt") else AgentDef(
        name="watcher", system_prompt=w["prompt"])
    me_name = policy().get("me", {}).get("name") or "the owner"
    verdict = await run_agent(
        agent,
        (f"# Guardrails (the owner's rules for this watcher)\n{w['guardrails'] or ''}\n\n"
         f"# New messages in '{w['name']}'\n"
         + json.dumps(fresh, ensure_ascii=False)
         + f"\n\n# Also\nMark needs_alex=true on anything {me_name} (the owner) personally "
           "must address today (questions to them, decisions waiting on them, their "
           "tickets blocked). When a short reply from the owner would move it forward, "
           "write suggested_reply the way the best agent in these chats writes: "
           "address the person and answer their ACTUAL question in the first "
           "sentence, then the nuance. Acknowledge good instincts ('good "
           "question - one nuance'). Be concrete - real ticket ids, PR numbers, "
           "behaviors - never vague. Close collaboratively when there's a next "
           "step ('happy to flip that if it matches how you work'). First "
           "person, short sentences, hyphens not em dashes, no sign-offs, no "
           "corporate fluff."),
        schema=WATCH_SCHEMA, with_tools=False, max_turns=4,
        label=f"watcher:{w['id']}", timeout=90)
    events = verdict.get("events") or []
    # follow-ups run as ephemeral workers (fresh context each), max 2 per pass
    checks = 0
    for ev in events:
        if checks >= 2 or (ev.get("check") or {}).get("kind") in (None, "none"):
            continue
        finding = await _spawn_check(w, ev)
        if finding:
            ev["checked"] = finding
        checks += 1
    # "reply for me": a suggested reply becomes a DRAFT in the Approvals queue
    # (HITL — nothing is sent until the user approves it on the board)
    for ev in events:
        reply = (ev.get("suggested_reply") or "").strip()
        if not (ev.get("needs_alex") and reply and w["kind"] == "teams_chat"):
            continue
        did = db.execute(
            "INSERT INTO drafts(item_id, channel, recipient, reply_to_ref, body, "
            "created_at) VALUES(?,?,?,?,?,?)",
            (None, "teams", w["name"], w["target"], reply, db.now()))
        db.audit("draft_created", {
            "source": "watcher", "watcher_id": w["id"], "draft_id": did,
            "conversation_id": w["target"], "headline": ev.get("headline", "")[:150],
        }, actor="watcher")
        ev["draft_id"] = did
    _emit_events(w, events)
    note = (f"{len(fresh)} new message{'s' if len(fresh) != 1 else ''} -> "
            f"{len(events)} event{'s' if len(events) != 1 else ''}"
            + (f" · {checks} ephemeral check{'s' if checks != 1 else ''}" if checks else ""))
    db.execute("UPDATE watchers SET cursor=?, last_run=?, last_note=?, updated_at=? "
               "WHERE id=?", (fresh[-1]["at"], db.now(), note, db.now(), w["id"]))
    log.info("watcher '%s': %s", w["name"], note)
    return note


def _watch_process(w: dict) -> str:
    rows = db.query(
        "SELECT * FROM job_runs WHERE job=? ORDER BY id DESC LIMIT 5", (w["target"],))
    if not rows:
        note = f"no runs recorded for job '{w['target']}'"
    else:
        streak = 0
        for r in rows:
            if r["ok"] == 0:
                streak += 1
            else:
                break
        if streak >= 2:
            _emit_events(w, [{
                "headline": f"job {w['target']} failing ({streak} in a row): "
                            f"{(rows[0]['error'] or '')[:100]}",
                "severity": "urgent" if streak >= 4 else "attention",
                "why": "process guardrail: repeated failures"}])
            note = f"failing x{streak}"
        elif rows[0]["ok"] == 0:
            note = "one recent failure — watching"
        else:
            note = "healthy"
    db.execute("UPDATE watchers SET last_run=?, last_note=?, updated_at=? WHERE id=?",
               (db.now(), note, db.now(), w["id"]))
    return note
