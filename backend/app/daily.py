"""Daily note duties: morning brief, evening shutdown, week backfill."""

import asyncio
import logging
from datetime import date, datetime, timedelta

from . import db, graph, state, vault
from .agents import CHIEF, run_agent

log = logging.getLogger("chief.daily")

TOP3_SCHEMA = {
    "type": "object",
    "properties": {
        "top3": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "also": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
    },
    "required": ["top3", "also"],
}

TOP3_PROMPT = """Propose a Top 3 for the user's day (plus up to 4 'also do' items) from these inputs.

RULES:
- Top 3 = highest-consequence items: things that BLOCK other people, VIP asks, hard deadlines.
- Urgency beats importance: if someone is waiting on the user right now, that goes Top 3.
- Dependabot / dependency-bump PRs = batch work; ONE 'also' item covers them all.
- Each item: short, actionable, verb-first. No em dashes. Max ~12 words.
- Tier C pings and pending drafts for VIPs belong in Top 3.
- If pending drafts ≥ 10 and the user hasn't recently been approving them, add "Review and approve pending drafts (N waiting)" to Also; if ≥ 15 and includes VIP drafts, put it in Top 3.
- Stale drafts (age_hours ≥ 24) signal an unanswered conversation; if any draft is ≥ 48h old, surface "Clear stale drafts — N pending ≥24h, oldest Xh" in Top 3 (blocking someone is blocking).
- If the user has PRs awaiting their review (review-requested), that belongs in Top 3 or Also. Any PR tagged [STALE Xd — escalate] is blocking the author; put it in Top 3 regardless of what else is there.
- Carry-forward (below) already exists as open checklist items and reappears in the note automatically — do NOT propose a new item that just restates one in different words. Only surface it again if its urgency has genuinely changed (e.g. now stale/escalating).

Carry-forward (unchecked from previous days):
{carry}

Open loops (aged, unresolved):
{loops}

PRs and sprint focus (open PRs, waiting on whom):
{focus}

Today's meetings:
{meetings}

Pending drafts awaiting the user's approval:
{pending_drafts}

Urgent / unanswered Teams pings (tier B or C, no reply sent):
{urgent_pings}

PRs awaiting the user's review (review-requested):
{prs_awaiting_review}
"""


async def morning_brief(target: date | None = None) -> dict:
    today = target or date.today()
    created = not vault.note_path(today).exists()

    carry: list[str] = []
    recent = vault.most_recent_note(before=today - timedelta(days=1))
    if recent:
        carry = vault.unchecked_items(recent[0])

    async def _get_calendar():
        try:
            return await graph.calendar_events_cached(today)
        except Exception as e:
            log.exception("calendar fetch failed; creating note without meetings")
            db.audit("job_error", {"job": "morning_brief.calendar", "error": str(e)[:300]})
            return []

    meetings, focus = await asyncio.gather(_get_calendar(), state.proposed_focus())
    meetings_lines = [
        f"{(m.get('start_local') or '')[11:16]} {m.get('subject')}" for m in meetings
    ]
    loops = db.query(
        "SELECT subject, content FROM items WHERE source='loops' "
        "AND status NOT IN ('dismissed','done') LIMIT 15")
    pending_drafts = db.query(
        "SELECT i.sender, i.subject, d.body, d.created_at, "
        "CAST(ROUND((julianday('now') - julianday(d.created_at)) * 24) AS INTEGER) as age_hours "
        "FROM drafts d "
        "LEFT JOIN items i ON i.id = d.item_id WHERE d.status='pending' "
        "ORDER BY COALESCE(i.urgent,0) DESC, d.created_at ASC LIMIT 8")
    urgent_pings = db.query(
        "SELECT sender, subject, content FROM items "
        "WHERE source IN ('teams_chat','teams_channel') AND tier IN ('B','C') "
        "AND status IN ('classified','drafted','held') "
        "AND (snoozed_until IS NULL OR snoozed_until <= ?) LIMIT 10", (db.now(),))
    # PRs where the user's review is specifically requested — include age for escalation
    prs_awaiting = db.query(
        "SELECT content, received_at, "
        "CAST(ROUND((julianday('now') - julianday(received_at))) AS INTEGER) as age_days "
        "FROM items WHERE source='pr_approval' AND status='classified' "
        "AND json_extract(content,'$.waiting_on')='you' LIMIT 10")

    proposed = {"top3": [], "also": []}
    try:
        import json as _json
        prs_lines = []
        for p in prs_awaiting:
            try:
                pr = _json.loads(p["content"] or "{}")
                age = p.get("age_days", 0) or 0
                age_tag = f" [STALE {age}d — escalate]" if age >= 7 else (f" [{age}d]" if age else "")
                prs_lines.append(f"- {pr.get('repo','')} #{pr.get('number','')} {pr.get('title','')[:60]}{age_tag}")
            except Exception:
                pass
        proposed = await run_agent(
            CHIEF,
            TOP3_PROMPT.format(
                carry="\n".join(f"- {c}" for c in carry) or "(none)",
                loops="\n".join(f"- {l['subject']}: {l['content'][:100]}" for l in loops) or "(none)",
                focus="\n".join(f"- [{f['kind']}] {f['title'][:100]}" for f in focus) or "(none)",
                meetings="\n".join(meetings_lines) or "(none)",
                pending_drafts="\n".join(
                    f"- {d['sender']} ({d['subject']}) [age_hours={d.get('age_hours',0)}]: "
                    f"{(d['body'] or '')[:80]}"
                    for d in pending_drafts) or "(none)",
                urgent_pings="\n".join(
                    f"- {p['sender']} in {p['subject']}: {(p['content'] or '')[:100]}"
                    for p in urgent_pings) or "(none)",
                prs_awaiting_review="\n".join(prs_lines) or "(none)",
            ),
            schema=TOP3_SCHEMA, with_tools=False, max_turns=4, label="morning_brief")
    except Exception as e:
        log.exception("top3 proposal failed")
        db.audit("job_error", {"job": "morning_brief.top3", "error": str(e)[:300]})

    if created:
        vault.create_note(today)
    note_text = vault.note_path(today).read_text()

    if created:
        if proposed["top3"]:
            block = "\n".join(f"- [ ] (proposed) {t}" for t in proposed["top3"])
            note_text = note_text.replace(
                "## Top 3 for Today\n\n- [ ] \n- [ ] \n- [ ] ",
                f"## Top 3 for Today\n\n{block}")
        if proposed["also"]:
            block = "\n".join(f"- [ ] (proposed) {t}" for t in proposed["also"])
            note_text = note_text.replace(
                "## Also Do Today\n\n- [ ] \n- [ ] ",
                f"## Also Do Today\n\n{block}")
        vault.note_path(today).write_text(note_text)

    if meetings_lines and "Meetings today:" not in note_text:
        vault.append_to_section(today, "Notes / Decisions Today",
                                "Meetings today:\n" + "\n".join(f"- {m}" for m in meetings_lines))
    if carry:
        existing = set(vault.unchecked_items(today))
        missing = [c for c in carry if c not in existing]
        if missing:
            vault.append_to_section(today, "Carry Forward",
                                    "\n".join(f"- [ ] {c}" for c in missing))

    # Post the brief to the user's Teams self-chat (priorities, meetings, tasks,
    # links, and this week's vision). Falls back gracefully if not resolvable.
    posted = "skipped"
    try:
        from . import actions
        html = _brief_html(today, proposed, meetings, carry, loops,
                           pending_drafts=pending_drafts, urgent_pings=urgent_pings)
        posted = await actions.notify_alex(html, kind="morning_brief")
    except actions.SendBlocked as e:
        posted = f"blocked: {e}"
    except Exception as e:
        log.exception("morning brief self-chat post failed")
        posted = f"error: {str(e)[:120]}"

    db.audit("morning_brief", {"date": today.isoformat(), "created": created,
                               "carried": len(carry), "proposed_top3": proposed["top3"],
                               "self_chat": posted})
    return {"date": today.isoformat(), "created": created, "carried": len(carry),
            "proposed_top3": proposed["top3"], "proposed_also": proposed["also"],
            "self_chat": posted}


async def ensure_daily_note(target: date | None = None) -> dict:
    """Safety net: guarantee today's daily note exists, even if the 08:00 morning
    brief never ran (app or Mac was off at the time). Creates a plain note from
    the template and carries forward unchecked items from the most recent note.
    No AI Top 3 proposal, no Teams post. Idempotent: a no-op if the note exists.
    Runs on app startup and on a daily weekday cron (see scheduler.py)."""
    today = target or date.today()
    if vault.note_path(today).exists():
        return {"date": today.isoformat(), "created": False}

    vault.create_note(today)

    carry: list[str] = []
    recent = vault.most_recent_note(before=today - timedelta(days=1))
    if recent:
        carry = vault.unchecked_items(recent[0])
        existing = set(vault.unchecked_items(today))
        missing = [c for c in carry if c not in existing]
        if missing:
            vault.append_to_section(today, "Carry Forward",
                                    "\n".join(f"- [ ] {c}" for c in missing))

    db.audit("daily_note_ensured", {"date": today.isoformat(), "carried": len(carry)})
    vault.chieff_trace("Calendar", f"created today's daily note ({today.isoformat()}) "
                                   "as a safety net (08:00 morning brief had not run)")
    log.info("daily note ensured for %s (carried %d)", today.isoformat(), len(carry))
    return {"date": today.isoformat(), "created": True, "carried": len(carry)}


def _esc(s) -> str:
    import html as _h
    return _h.escape(str(s or ""), quote=True)


def _weekly_vision() -> str:
    """A short 'this week' block from the roadmap (here-week + callouts) and the
    latest weekly status, for the morning brief."""
    import json
    bits = []
    raw = db.get_setting("os_conversions_roadmap")
    if raw:
        try:
            rm = json.loads(raw)
            here = rm.get("here_week", "")
            sub = rm.get("subtitle", "")
            rides = (rm.get("callouts") or {}).get("rides_on") or []
            bits.append(f"<p><b>This week ({_esc(here)})</b> &mdash; {_esc(sub)}</p>")
            if rides:
                bits.append("<p>Rides on:</p><ul>"
                            + "".join(f"<li>{_esc(r)}</li>" for r in rides[:3]) + "</ul>")
        except Exception:
            pass
    return "".join(bits)


def _brief_html(today: date, proposed: dict, meetings: list, carry: list, loops: list,
                pending_drafts: list | None = None, urgent_pings: list | None = None) -> str:
    import json
    emoji = "☀️"
    p = [f"<p>{emoji} <b>Morning brief &mdash; {today.strftime('%a %d %b')}</b></p>"]

    top3 = proposed.get("top3") or []
    if top3:
        p.append("<p><b>Top 3 today</b></p><ol>"
                 + "".join(f"<li>{_esc(t)}</li>" for t in top3) + "</ol>")
    also = proposed.get("also") or []
    if also:
        p.append("<p><b>Also</b></p><ul>"
                 + "".join(f"<li>{_esc(t)}</li>" for t in also) + "</ul>")

    if meetings:
        rows = []
        for m in meetings:
            t = (m.get("start_local") or "")[11:16]
            subj = _esc(m.get("subject"))
            link = m.get("webLink")
            subj = f'<a href="{link}">{subj}</a>' if link else subj
            rows.append(f"<li>{t} &middot; {subj}</li>")
        p.append("<p><b>Meetings</b></p><ul>" + "".join(rows) + "</ul>")

    if carry:
        p.append("<p><b>Carry-forward tasks</b></p><ul>"
                 + "".join(f"<li>{_esc(c)}</li>" for c in carry[:8]) + "</ul>")

    # PR readiness + open loops at a glance
    flags = []
    try:
        pr = json.loads(db.get_setting("pr_readiness_state") or "{}").get("prs", [])
        na = [r for r in pr if r.get("needs_attention")]
        if na:
            flags.append(f"{len(na)} PR(s) not ready: "
                         + ", ".join(_esc(r.get("title", ""))[:30] for r in na[:3]))
    except Exception:
        pass
    if loops:
        flags.append(f"{len(loops)} open loop(s)")
    if pending_drafts:
        flags.insert(0, f'{len(pending_drafts)} draft(s) awaiting approval &mdash; '
                        f'<a href="http://localhost:5173">review on board</a>')
    if urgent_pings:
        flags.append(f"{len(urgent_pings)} urgent ping(s) needing a reply")
    if flags:
        p.append("<p><b>Watch</b></p><ul>" + "".join(f"<li>{f}</li>" for f in flags) + "</ul>")

    vision = _weekly_vision()
    if vision:
        p.append(vision)

    p.append('<p><b>Links</b> &middot; <a href="http://localhost:5173">Control tower</a> '
             '&middot; <a href="http://localhost:5173/#weekly">Weekly status</a> '
             '&middot; <a href="http://localhost:5173/#roadmap">Roadmap</a></p>')
    return "".join(p)


async def evening_shutdown(target: date | None = None) -> dict:
    today = target or date.today()
    if not vault.note_path(today).exists():
        await morning_brief(today)

    midnight_utc = datetime.combine(today, datetime.min.time()).astimezone().isoformat()

    # Merge two audit_log queries into one to save a DB round-trip.
    audit_rows = db.query(
        "SELECT action, detail FROM audit_log "
        "WHERE action IN ('approved_send','loop_closed') AND ts > ?", (midnight_utc,))
    sent = [r for r in audit_rows if r["action"] == "approved_send"]
    closed_loops = [r for r in audit_rows if r["action"] == "loop_closed"]
    answered = db.query(
        "SELECT sender, subject FROM items WHERE status='answered' AND updated_at > ?",
        (midnight_utc,))
    note = vault.read_note(today)
    done_today, still_open = [], []
    if note:
        for sec in ("Top 3 for Today", "Also Do Today", "Carry Forward"):
            for cb in note["sections"].get(sec, {}).get("checkboxes", []):
                (done_today if cb["checked"] else still_open).append(cb["text"])

    snap = db.query(
        "SELECT (SELECT COUNT(*) FROM drafts WHERE status='pending') AS pending_drafts, "
        "       (SELECT COUNT(*) FROM items WHERE type='open_loop' AND status!='done') AS open_loops")[0]
    pending_drafts = snap["pending_drafts"]
    open_loops = snap["open_loops"]
    dropped = db.query(
        "SELECT sender, substr(content,1,80) AS c FROM items WHERE source='teams_chat' "
        "AND tier IN ('B','C') AND status IN ('classified','drafted','held') "
        "AND received_at > ? LIMIT 8", (midnight_utc,))

    lines = [f"Shutdown summary ({datetime.now().strftime('%H:%M')}):"]
    lines.append(f"- Closed today: {len(done_today)} checkboxes"
                 + (f" ({', '.join(done_today[:5])})" if done_today else "")
                 + f", {len(answered)} threads answered, {len(closed_loops)} loops closed, {len(sent)} approved sends")
    lines.append(f"- Still open: {len(still_open)} checkboxes, {pending_drafts} drafts awaiting approval, {open_loops} open loops")
    if dropped:
        lines.append("- Possibly dropped (B/C pings today with no reply yet):")
        lines += [f"  - {d['sender']}: {d['c']}" for d in dropped]

    vault.append_to_section(today, "Notes / Decisions Today", "\n".join(lines))

    # Post a formatted summary to the user's Teams self-chat (mirrors morning brief)
    shutdown_html = _shutdown_html(today, done_today, still_open, answered,
                                   len(closed_loops), len(sent), pending_drafts,
                                   open_loops, dropped)
    posted = "skipped"
    try:
        from . import actions
        posted = await actions.notify_alex(shutdown_html, kind="evening_shutdown")
    except Exception as e:
        log.warning("evening shutdown self-chat post failed: %s", str(e)[:120])
        posted = f"error: {str(e)[:120]}"

    db.audit("evening_shutdown", {"date": today.isoformat(), "done": len(done_today),
                                  "open": len(still_open), "dropped": len(dropped),
                                  "self_chat": posted})
    return {"date": today.isoformat(), "done": len(done_today), "still_open": len(still_open),
            "dropped": [f"{d['sender']}: {d['c']}" for d in dropped], "self_chat": posted}


def _shutdown_html(today: date, done: list, still_open: list, answered: list,
                   loops_closed: int, sends: int, pending_drafts: int,
                   open_loops: int, dropped: list) -> str:
    p = [f"<p>🌆 <b>Evening shutdown &mdash; {today.strftime('%a %d %b')}</b></p>"]

    wins = []
    if done:
        wins.append(f"{len(done)} task{'s' if len(done)!=1 else ''} done")
    if answered:
        wins.append(f"{len(answered)} thread{'s' if len(answered)!=1 else ''} answered")
    if sends:
        wins.append(f"{sends} send{'s' if sends!=1 else ''} approved")
    if loops_closed:
        wins.append(f"{loops_closed} loop{'s' if loops_closed!=1 else ''} closed")
    if wins:
        p.append("<p><b>Done today</b></p><ul>"
                 + "".join(f"<li>{_esc(w)}</li>" for w in wins) + "</ul>")

    if done:
        p.append("<p><b>Completed tasks</b></p><ul>"
                 + "".join(f"<li>{_esc(t)}</li>" for t in done[:6]) + "</ul>")

    carries = []
    if still_open:
        carries.append(f"{len(still_open)} task(s) not yet done")
    if pending_drafts:
        carries.append(f"{pending_drafts} draft(s) awaiting approval "
                       f'&mdash; <a href="http://localhost:5173">review on board</a>')
    if open_loops:
        carries.append(f"{open_loops} open loop(s)")
    if carries:
        p.append("<p><b>Still open</b></p><ul>"
                 + "".join(f"<li>{_esc(c) if 'href' not in c else c}</li>" for c in carries)
                 + "</ul>")

    if dropped:
        p.append("<p><b>Possibly dropped (B/C, no reply)</b></p><ul>"
                 + "".join(f"<li>{_esc(d['sender'])}: {_esc(d['c'])}</li>" for d in dropped[:5])
                 + "</ul>")

    p.append('<p><b>Links</b> &middot; <a href="http://localhost:5173">Control tower</a></p>')
    return "".join(p)


async def backfill_week() -> dict:
    """Minimal reconstructed notes for skipped weekdays this week, so the chain is unbroken."""
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    filled = []
    d = monday
    while d < today:
        if d.weekday() < 5 and not vault.note_path(d).exists():
            events = await graph.calendar_events_cached(d)
            lines = [f"# {d.isoformat()} {d.strftime('%A')}", "",
                     "> Reconstructed from calendar/email/Teams by Chief (the user did not write this note).",
                     "", "## Top 3 for Today", "", "## Also Do Today", "",
                     "## Notes / Decisions Today", ""]
            if events:
                lines.append("Meetings that day:")
                lines += [f"- {(e.get('start_local') or '')[11:16]} {e.get('subject')}" for e in events]
            lines += ["", "## Carry Forward", ""]
            vault.create_note(d, body="\n".join(lines) + "\n")
            filled.append(d.isoformat())
            db.audit("note_backfilled", {"date": d.isoformat(), "meetings": len(events)})
        d += timedelta(days=1)
    return {"backfilled": filled}
