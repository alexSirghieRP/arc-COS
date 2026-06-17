"""Rough-timeline roadmap: data model, agent schema, and a
Confluence-storage renderer that draws a swimlane Gantt + legend + dependency
table in the style of the ARC Intake MVA timeline.

CoS drafts a roadmap from available data (ADO + PRs + notes); the user edits/adds
(HITL) and approves; the approved roadmap renders into a standing Confluence page.
"""

import html

# status -> (background, foreground, legend label)
STATUS = {
    "done":      ("#36B37E", "#06281a", "Done"),
    "build":     ("#4C9AFF", "#08233f", "Build"),
    "spike":     ("#FFAB00", "#3d2a00", "Spike / prep / hardening"),
    "tbd":       ("#C1C7D0", "#2b2f36", "Unknown / TBD"),
}
MILESTONE_COLOR = "#6554C0"
GATE_COLOR = "#FF5630"

_BAR = {
    "type": "object",
    "properties": {
        "start": {"type": "string", "description": "start week label, e.g. 'W25'"},
        "end": {"type": "string", "description": "end week label (inclusive)"},
        "status": {"type": "string", "enum": ["done", "build", "spike", "tbd"]},
        "label": {"type": "string", "description": "short bar label"},
    },
    "required": ["start", "end", "status", "label"],
}

# JSON schema the drafting agent fills in / updates (then a human edits it).
ROADMAP_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "subtitle": {"type": "string"},
        "callouts": {
            "type": "object",
            "properties": {
                "what_changed": {"type": "array", "items": {"type": "string"},
                                 "description": "what moved since last review, from PRs/notes"},
                "rides_on": {"type": "array", "items": {"type": "string"},
                             "description": "risks/dependencies the dates ride on"},
            },
        },
        "weeks": {"type": "array", "items": {"type": "string"},
                  "description": "ordered week column labels covering the plan window"},
        "months": {"type": "array", "items": {
            "type": "object",
            "properties": {"label": {"type": "string"}, "span": {"type": "integer"}},
            "required": ["label", "span"]},
            "description": "month headers; spans must sum to len(weeks)"},
        "here_week": {"type": "string", "description": "the current 'you are here' week"},
        "sections": {
            "type": "array",
            "description": "workstream groups (swimlane sections)",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "owner": {"type": "string"},
                    "lanes": {"type": "array", "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "bars": {"type": "array", "items": _BAR},
                        },
                        "required": ["name", "bars"]}},
                },
                "required": ["name", "lanes"],
            },
        },
        "milestones": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "week": {"type": "string"},
                "label": {"type": "string"},
                "gate": {"type": "boolean", "description": "true = hard gate"},
            },
            "required": ["week", "label"]}},
        "footer": {"type": "string"},
    },
    "required": ["title", "weeks", "here_week", "sections"],
}


SETTING_KEY = "os_conversions_roadmap"

# Seed so the control-tower preview renders before the agent drafts a real one.
# Grouped sections with owners + week-positioned bars, mirroring the design.
SEED = {
    "title": "Rough Timeline",
    "subtitle": "Example roadmap · edit on the board or run 'Draft from data'",
    "callouts": {
        "what_changed": [
            "This is placeholder content so the preview renders out of the box.",
            "Click 'Draft from data' to generate from your PRs / ADO / notes, or 'Edit' to write your own.",
        ],
        "rides_on": [
            "Define the risks/dependencies your dates ride on here.",
        ],
    },
    "weeks": ["W1", "W2", "W3", "W4", "W5", "W6", "W7", "W8"],
    "months": [{"label": "Month 1", "span": 4}, {"label": "Month 2", "span": 4}],
    "here_week": "W3",
    "sections": [
        {"name": "Workstream A", "owner": "Owner A", "lanes": [
            {"name": "Build the thing", "bars": [
                {"start": "W1", "end": "W2", "status": "done", "label": "design"},
                {"start": "W3", "end": "W5", "status": "build", "label": "implementation"}]},
        ]},
        {"name": "Workstream B", "owner": "Owner B", "lanes": [
            {"name": "Integrate", "bars": [
                {"start": "W4", "end": "W6", "status": "spike", "label": "spike / hardening"}]},
        ]},
        {"name": "UAT & cutover", "owner": "All", "lanes": [
            {"name": "Test -> ship", "bars": [
                {"start": "W7", "end": "W8", "status": "tbd", "label": "cutover prep"}]},
        ]},
    ],
    "milestones": [
        {"week": "W4", "label": "Checkpoint", "gate": False},
        {"week": "W8", "label": "Go-live (hard)", "gate": True},
    ],
    "footer": "Rough order-of-magnitude, not a committed date. Drafted by CoS; reviewed by you.",
}


def _esc(s) -> str:
    return html.escape(str(s or ""), quote=True)


def _idx(weeks: list[str]) -> dict:
    return {w: i for i, w in enumerate(weeks)}


def _lane_row(lane: dict, weeks: list[str], wi: dict) -> str:
    """One swimlane: lane name + week cells, consecutive same-bar weeks merged."""
    # map each week index -> (bar) covering it (last bar wins on overlap)
    cover: list[dict | None] = [None] * len(weeks)
    for b in lane.get("bars", []):
        s, e = wi.get(b.get("start")), wi.get(b.get("end"))
        if s is None or e is None:
            continue
        for i in range(min(s, e), max(s, e) + 1):
            cover[i] = b
    cells = [f'<td style="font-size:11px;padding:3px 6px;">{_esc(lane["name"])}</td>']
    i = 0
    while i < len(weeks):
        b = cover[i]
        if b is None:
            cells.append('<td></td>')
            i += 1
            continue
        j = i
        while j < len(weeks) and cover[j] is b:
            j += 1
        bg, fg, _ = STATUS.get(b.get("status", "tbd"), STATUS["tbd"])
        cells.append(
            f'<td colspan="{j - i}" style="background-color:{bg};color:{fg};'
            f'text-align:center;font-size:10px;padding:3px;border-radius:3px;">'
            f'{_esc(b.get("label", ""))}</td>')
        i = j
    return "<tr>" + "".join(cells) + "</tr>"


def _header_row(weeks: list[str], here: str) -> str:
    cells = ['<th style="font-size:10px;">Lane</th>']
    for w in weeks:
        mark = ' style="background-color:#172B4D;color:#fff;font-size:10px;"' if w == here \
            else ' style="font-size:10px;color:#5e6c84;"'
        cells.append(f'<th{mark}>{_esc(w)}</th>')
    return "<tr>" + "".join(cells) + "</tr>"


def _milestone_row(milestones: list[dict], weeks: list[str], wi: dict) -> str:
    if not milestones:
        return ""
    cells = ['<td style="font-size:11px;padding:3px 6px;"><em>Milestones</em></td>']
    by_week = {}
    for m in milestones:
        if m.get("week") in wi:
            by_week.setdefault(m["week"], []).append(m)
    for w in weeks:
        ms = by_week.get(w)
        if ms:
            gate = any(m.get("gate") for m in ms)
            color = GATE_COLOR if gate else MILESTONE_COLOR
            label = "; ".join(_esc(m.get("label", "")) for m in ms)
            cells.append(f'<td style="text-align:center;color:{color};font-size:10px;" '
                         f'title="{label}">{"❖" if gate else "◆"}</td>')
        else:
            cells.append("<td></td>")
    return "<tr>" + "".join(cells) + "</tr>"


def _legend() -> str:
    chips = []
    for key in ("done", "build", "spike", "tbd"):
        bg, fg, label = STATUS[key]
        chips.append(f'<span style="background-color:{bg};color:{fg};padding:1px 8px;'
                     f'border-radius:3px;font-size:11px;margin-right:6px;">{label}</span>')
    chips.append(f'<span style="color:{MILESTONE_COLOR};font-size:12px;margin-right:6px;">'
                 f'◆ Milestone</span>')
    chips.append(f'<span style="color:{GATE_COLOR};font-size:12px;">❖ Hard gate</span>')
    return "<p>" + "".join(chips) + "</p>"


def _dependency_table(lanes: list[dict]) -> str:
    rows = ['<table><tbody><tr><th>Lane / workstream</th><th>Owner / effort</th>'
            '<th>Notes / dependencies</th></tr>']
    for ln in lanes:
        rows.append(f'<tr><td>{_esc(ln.get("name"))}</td>'
                    f'<td>{_esc(ln.get("effort", ""))}</td>'
                    f'<td>{_esc(ln.get("notes", ""))}</td></tr>')
    rows.append("</tbody></table>")
    return "".join(rows)


def render_storage(rm: dict) -> str:
    """Roadmap dict -> Confluence storage HTML (Gantt + legend + dependency table)."""
    weeks = rm.get("weeks", [])
    wi = _idx(weeks)
    here = rm.get("here_week", "")
    parts = [f'<h1>{_esc(rm.get("title", "Rough Timeline"))}</h1>']
    if rm.get("subtitle"):
        parts.append(f'<p><strong>{_esc(rm["subtitle"])}</strong></p>')
    if rm.get("summary"):
        parts.append(f'<blockquote><p>{_esc(rm["summary"])}</p></blockquote>')
    parts.append(_legend())
    # the swimlane gantt, grouped by section
    parts.append('<table><tbody>')
    parts.append(_header_row(weeks, here))
    sections = rm.get("sections")
    if sections:
        ncols = len(weeks) + 1
        for sec in sections:
            owner = f' &middot; {_esc(sec.get("owner"))}' if sec.get("owner") else ""
            parts.append(f'<tr><td colspan="{ncols}" style="background-color:#172B4D;'
                         f'color:#fff;font-size:11px;font-weight:600;padding:3px 6px;">'
                         f'{_esc(sec.get("name"))}{owner}</td></tr>')
            for lane in sec.get("lanes", []):
                parts.append(_lane_row(lane, weeks, wi))
    else:  # legacy flat lanes
        for lane in rm.get("lanes", []):
            parts.append(_lane_row(lane, weeks, wi))
    ms = _milestone_row(rm.get("milestones", []), weeks, wi)
    if ms:
        parts.append(ms)
    parts.append('</tbody></table>')
    # dependency / workstream table
    parts.append('<h2>Lanes &amp; ownership</h2>')
    lane_rows = []
    for sec in (sections or []):
        lane_rows.append({"name": sec.get("name"), "effort": sec.get("owner", ""),
                          "notes": " &middot; ".join(l.get("name", "") for l in sec.get("lanes", []))})
    parts.append(_dependency_table(lane_rows or rm.get("lanes", [])))
    if rm.get("footer"):
        parts.append(f'<p><em>{_esc(rm["footer"])}</em></p>')
    return "".join(parts)


def _print_bars_track(lane: dict, weeks: list[str], wi: dict) -> str:
    n = len(weeks)
    cells = [f'<div style="grid-column:2 / span {n};display:grid;'
             f'grid-template-columns:repeat({n},1fr);align-items:center;">']
    for b in lane.get("bars", []):
        s, e = wi.get(b.get("start")), wi.get(b.get("end"))
        if s is None or e is None:
            continue
        bg, fg, _ = STATUS.get(b.get("status", "tbd"), STATUS["tbd"])
        cells.append(f'<div style="grid-column:{min(s,e)+1} / {max(s,e)+2};'
                     f'background:{bg};color:{fg};border-radius:4px;text-align:center;'
                     f'font-size:9px;padding:4px 2px;margin:2px 0;overflow:hidden;">'
                     f'{_esc(b.get("label",""))}</div>')
    cells.append('</div>')
    return "".join(cells)


def render_print_html(rm: dict) -> str:
    """Standalone LIGHT, print-ready HTML of the roadmap, matching the
    design-blueprint look. Rendered to PDF by Playwright (landscape)."""
    weeks = rm.get("weeks", [])
    wi = _idx(weeks)
    here = rm.get("here_week", "")
    n = len(weeks)
    gcol = f"display:grid;grid-template-columns:200px repeat({n},1fr);"

    p = ['<!doctype html><html><head><meta charset="utf-8"><style>',
         "*{box-sizing:border-box}"
         "body{font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;color:#172B4D;"
         "margin:26px;font-size:11px;}"
         "h1{font-size:19px;margin:0 0 1px;} .sub{color:#5e6c84;margin:0 0 12px;}"
         ".callouts{display:flex;gap:10px;margin:0 0 14px;}"
         ".callout{flex:1;border-radius:8px;padding:8px 11px;}"
         ".callout h4{margin:0 0 4px;font-size:10px;text-transform:uppercase;letter-spacing:.04em;}"
         ".callout ul{margin:0;padding-left:15px;} .callout li{margin:1px 0;font-size:10px;}"
         ".cw{background:#f4f0ff;border:1px solid #ddd1ff;} .cw h4{color:#5e3bb7;}"
         ".cr{background:#fff8ec;border:1px solid #ffe3ad;} .cr h4{color:#9a6b00;}"
         ".sec{background:#172B4D;color:#fff;font-size:10px;font-weight:600;"
         "padding:3px 8px;border-radius:4px;margin-top:7px;letter-spacing:.03em;}"
         ".lane{font-size:10px;color:#42526e;padding:2px 6px;}"
         ".wk{text-align:center;font-size:9px;color:#7a869a;}"
         ".wk.here{background:#ffe5e5;color:#c9372c;font-weight:700;border-radius:3px 3px 0 0;}"
         ".mo{text-align:center;font-size:10px;font-weight:600;text-transform:uppercase;"
         "color:#5e6c84;border-bottom:1px solid #dfe1e6;padding-bottom:1px;}"
         ".legend{margin:12px 0;} .legend span{padding:1px 8px;border-radius:3px;"
         "margin-right:7px;font-size:10px;}"
         "table{border-collapse:collapse;width:100%;font-size:10px;margin-top:6px;}"
         "th{text-align:left;text-transform:uppercase;font-size:9px;color:#7a869a;"
         "border-bottom:2px solid #dfe1e6;padding:4px 6px;}"
         "td{border-bottom:1px solid #ebecf0;padding:4px 6px;vertical-align:top;}"
         ".foot{color:#97a0af;font-size:9px;margin-top:10px;font-style:italic;}"
         "</style></head><body>"]
    p.append(f'<h1>{_esc(rm.get("title","Rough Timeline"))}</h1>')
    if rm.get("subtitle"):
        p.append(f'<div class="sub">{_esc(rm["subtitle"])}</div>')
    co = rm.get("callouts") or {}
    if co.get("what_changed") or co.get("rides_on"):
        p.append('<div class="callouts">')
        if co.get("what_changed"):
            p.append('<div class="callout cw"><h4>What changed</h4><ul>'
                     + "".join(f"<li>{_esc(x)}</li>" for x in co["what_changed"]) + "</ul></div>")
        if co.get("rides_on"):
            p.append('<div class="callout cr"><h4>What it rides on</h4><ul>'
                     + "".join(f"<li>{_esc(x)}</li>" for x in co["rides_on"]) + "</ul></div>")
        p.append('</div>')
    # gantt wrapper (relative) so the 'today' line can span all rows
    p.append('<div style="position:relative;">')
    # month header
    p.append(f'<div style="{gcol}"><div></div>')
    for m in (rm.get("months") or []):
        p.append(f'<div class="mo" style="grid-column:span {m.get("span",1)};">{_esc(m.get("label"))}</div>')
    p.append('</div>')
    # week header
    p.append(f'<div style="{gcol}"><div></div>')
    for w in weeks:
        cls = "wk here" if w == here else "wk"
        p.append(f'<div class="{cls}">{"▼" if w==here else ""}{_esc(w)}</div>')
    p.append('</div>')
    # swimlanes
    for sec in (rm.get("sections") or []):
        owner = f' · {_esc(sec.get("owner"))}' if sec.get("owner") else ""
        p.append(f'<div class="sec">{_esc(sec.get("name"))}{owner}</div>')
        for lane in sec.get("lanes", []):
            p.append(f'<div style="{gcol}align-items:center;">'
                     f'<div class="lane">{_esc(lane.get("name"))}</div>'
                     + _print_bars_track(lane, weeks, wi) + '</div>')
    # milestones
    if rm.get("milestones"):
        p.append(f'<div style="{gcol}align-items:center;border-top:1px solid #dfe1e6;margin-top:4px;">'
                 f'<div class="lane"><em>Milestones</em></div>')
        for w in weeks:
            ms = [m for m in rm["milestones"] if m.get("week") == w]
            if ms:
                color = GATE_COLOR if any(m.get("gate") for m in ms) else MILESTONE_COLOR
                p.append(f'<div style="text-align:center;color:{color};font-size:11px;" '
                         f'title="{_esc("; ".join(m.get("label","") for m in ms))}">◆</div>')
            else:
                p.append('<div></div>')
        p.append('</div>')
    # 'today' vertical dashed line across the gantt (matches the UI)
    here_idx = wi.get(here, -1)
    if here_idx >= 0 and n:
        left = f"calc(200px + (100% - 200px) * {(here_idx + 0.5) / n})"
        p.append(f'<div style="position:absolute;top:14px;bottom:0;left:{left};'
                 f'border-left:2px dashed {GATE_COLOR};opacity:.8;"></div>')
    p.append('</div>')  # close gantt wrapper
    # legend
    p.append('<div class="legend">')
    for k in ("done", "build", "spike", "tbd"):
        bg, fg, label = STATUS[k]
        p.append(f'<span style="background:{bg};color:{fg};">{label}</span>')
    p.append(f'<span style="color:{MILESTONE_COLOR};">◆ Milestone</span>'
             f'<span style="color:{GATE_COLOR};">◆ Hard gate</span></div>')
    # dependency table
    p.append('<table><tr><th>Lane / workstream</th><th>Owner</th><th>Notes</th></tr>')
    for sec in (rm.get("sections") or []):
        p.append(f'<tr><td>{_esc(sec.get("name"))}</td><td>{_esc(sec.get("owner",""))}</td>'
                 f'<td>{_esc(" · ".join(l.get("name","") for l in sec.get("lanes",[])))}</td></tr>')
    p.append('</table>')
    if rm.get("footer"):
        p.append(f'<div class="foot">{_esc(rm["footer"])}</div>')
    p.append('</body></html>')
    return "".join(p)


async def to_pdf(rm: dict) -> bytes:
    """Render the print HTML to a landscape PDF via headless Chromium."""
    from playwright.async_api import async_playwright
    html_doc = render_print_html(rm)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            page = await browser.new_page()
            await page.set_content(html_doc, wait_until="networkidle")
            pdf = await page.pdf(format="A3", landscape=True, print_background=True,
                                 margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
        finally:
            await browser.close()
    return pdf


DRAFT_PROMPT = """You maintain the user's rough-timeline roadmap (a swimlane Gantt).
UPDATE the current roadmap below using this week's evidence. Preserve the human's
structure and edits; do not invent work. Rules:
- Advance "here_week" to the current week ({here}).
- Flip bars to status "done" when the evidence shows that work merged/landed; keep
  in-flight work as "build", investigations/prep as "spike", and not-yet-scoped as "tbd".
- Refresh callouts.what_changed (3-5 bullets from merged PRs / notes) and callouts.rides_on
  (top risks/blockers). No em dashes.
- Keep weeks/months consistent (month spans sum to number of weeks). Keep it rough.
Return the FULL updated roadmap JSON.

=== CURRENT ROADMAP ===
{current}

=== MERGED PRs (recent) ===
{prs}

=== ADO WORK ITEMS (the user) ===
{ado}

=== NOTES / CHIEFF LOG (this & last week) ===
{notes}
"""


async def draft() -> dict:
    """Draft/refresh the roadmap from available data (PRs + ADO + notes), then
    persist it. The human reviews/edits on the board before it ships (HITL)."""
    import asyncio
    import json
    from datetime import date
    from . import ado, db
    from .agents import CHIEF, run_agent
    from .sweeps import weekly_status as ws

    raw = db.get_setting(SETTING_KEY)
    current = json.loads(raw) if raw else SEED
    meta = ws._week_meta(date.today())
    here = meta["week_label"].replace("Week ", "W")

    prs = await ws._gather_prs(meta, {"base_branch": "dev"})
    notes = await asyncio.to_thread(ws._gather_context, meta)
    try:
        ado_items = ado.my_items().get("items", [])[:30]
        ado_txt = "\n".join(f"- {i['type']} {i['id']} [{i['state']}] {i['title']}"
                            for i in ado_items) or "(none)"
    except Exception:
        ado_txt = "(ADO unavailable)"

    prompt = DRAFT_PROMPT.format(
        here=here, current=json.dumps(current, indent=1),
        prs="\n".join(prs.get("brief", [])) or "(no merged PRs this week)",
        ado=ado_txt, notes=notes[:8000])
    result = await run_agent(CHIEF, prompt, schema=ROADMAP_SCHEMA, with_tools=False,
                             max_turns=8, model="claude-sonnet-4-6", label="roadmap")
    if not isinstance(result, dict):
        raise RuntimeError("roadmap draft produced no structured result")
    db.set_setting(SETTING_KEY, json.dumps(result))
    db.audit("roadmap_drafted", {"here": here, "sections": len(result.get("sections", []))})
    from . import vault
    vault.chieff_trace("Confluence", f"roadmap drafted from data ({here}, "
                                     f"{len(result.get('sections', []))} sections)")
    return {"roadmap": result, "saved": True, "drafted": True}
