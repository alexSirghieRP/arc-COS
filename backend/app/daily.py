"""Daily note duties: morning brief, evening shutdown, week backfill."""

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
Pick what is genuinely most consequential: things blocking others, deadlines, VIP threads.
Dependabot/dependency-bump PRs are batch work, not Top 3 material; if several exist, one
'also' item like "batch-review dependabot PRs in <repo>" is enough. Keep each item short,
actionable, starting with a verb. No em dashes.

Carry-forward (unchecked from previous days):
{carry}

Open loops:
{loops}

PRs and sprint focus:
{focus}

Today's meetings:
{meetings}
"""


async def morning_brief(target: date | None = None) -> dict:
    today = target or date.today()
    created = not vault.note_path(today).exists()

    carry: list[str] = []
    recent = vault.most_recent_note(before=today - timedelta(days=1))
    if recent:
        carry = vault.unchecked_items(recent[0])

    meetings = await graph.calendar_events(today)
    meetings_lines = [
        f"{(m.get('start_local') or '')[11:16]} {m.get('subject')}" for m in meetings
    ]
    loops = db.query(
        "SELECT subject, content FROM items WHERE type='open_loop' AND status!='done' LIMIT 20")
    focus = await state.proposed_focus()

    proposed = {"top3": [], "also": []}
    try:
        proposed = await run_agent(
            CHIEF,
            TOP3_PROMPT.format(
                carry="\n".join(f"- {c}" for c in carry) or "(none)",
                loops="\n".join(f"- {l['subject']}: {l['content'][:100]}" for l in loops) or "(none)",
                focus="\n".join(f"- [{f['kind']}] {f['title'][:100]}" for f in focus) or "(none)",
                meetings="\n".join(meetings_lines) or "(none)",
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
        html = _brief_html(today, proposed, meetings, carry, loops)
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


def _brief_html(today: date, proposed: dict, meetings: list, carry: list, loops: list) -> str:
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
    if flags:
        p.append("<p><b>Watch</b></p><ul>" + "".join(f"<li>{f}</li>" for f in flags) + "</ul>")

    vision = _weekly_vision()
    if vision:
        p.append(vision)

    p.append('<p><b>Links</b> &middot; <a href="http://localhost:7777">Control tower</a> '
             '&middot; <a href="http://localhost:7777/#weekly">Weekly status</a> '
             '&middot; <a href="http://localhost:7777/#roadmap">Roadmap</a></p>')
    return "".join(p)


async def evening_shutdown(target: date | None = None) -> dict:
    today = target or date.today()
    if not vault.note_path(today).exists():
        await morning_brief(today)

    midnight_utc = datetime.combine(today, datetime.min.time()).astimezone().isoformat()

    sent = db.query(
        "SELECT detail FROM audit_log WHERE action='approved_send' AND ts > ?", (midnight_utc,))
    answered = db.query(
        "SELECT sender, subject FROM items WHERE status='answered' AND updated_at > ?",
        (midnight_utc,))
    closed_loops = db.query(
        "SELECT detail FROM audit_log WHERE action='loop_closed' AND ts > ?", (midnight_utc,))
    note = vault.read_note(today)
    done_today, still_open = [], []
    if note:
        for sec in ("Top 3 for Today", "Also Do Today", "Carry Forward"):
            for cb in note["sections"].get(sec, {}).get("checkboxes", []):
                (done_today if cb["checked"] else still_open).append(cb["text"])

    pending_drafts = db.query("SELECT COUNT(*) AS n FROM drafts WHERE status='pending'")[0]["n"]
    open_loops = db.query(
        "SELECT COUNT(*) AS n FROM items WHERE type='open_loop' AND status!='done'")[0]["n"]
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
    db.audit("evening_shutdown", {"date": today.isoformat(), "done": len(done_today),
                                  "open": len(still_open), "dropped": len(dropped)})
    return {"date": today.isoformat(), "done": len(done_today), "still_open": len(still_open),
            "dropped": [f"{d['sender']}: {d['c']}" for d in dropped]}


async def backfill_week() -> dict:
    """Minimal reconstructed notes for skipped weekdays this week, so the chain is unbroken."""
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    filled = []
    d = monday
    while d < today:
        if d.weekday() < 5 and not vault.note_path(d).exists():
            events = await graph.calendar_events(d)
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
