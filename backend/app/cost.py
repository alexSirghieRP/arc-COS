"""LLM usage cost tracking from local CLI logs.

Claude: per-message token usage from ~/.claude/projects/**/*.jsonl, priced at
current Anthropic rates (verified via the claude-api reference, 2026-06).
Codex: cumulative per-session usage from ~/.codex/sessions/**/*.jsonl; OpenAI
rates are configurable (cost.openai_prices in policy.yaml) since they aren't
hardcoded — tokens always show, cost shows once rates are set.

Scope: this is CLI usage on THIS machine, the best signal available without an
Anthropic Admin / OpenAI billing API key. Not a billing-accurate org figure.
"""

import html
import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .config import policy

log = logging.getLogger("chief.cost")

# Anthropic price per token (input, output), by model-name fragment. Checked in
# order, so a more specific fragment (e.g. "sonnet-5") must precede a fragment
# it's a superset of (e.g. "sonnet") or the generic one always wins.
CLAUDE_PRICES = {
    "fable": (10/1e6, 50/1e6), "mythos": (10/1e6, 50/1e6),
    "opus": (5/1e6, 25/1e6),
    "sonnet-5": (2/1e6, 10/1e6),  # Sonnet 5 introductory rate; see SONNET5_INTRO_END
    "sonnet": (3/1e6, 15/1e6), "haiku": (1/1e6, 5/1e6),
}
CACHE_READ_MULT = 0.1
CACHE_WRITE_5M = 1.25
CACHE_WRITE_1H = 2.0

# Sonnet 5 launched at an introductory $2/$10-per-MTok rate through this date;
# after it, Sonnet 5 bills at the standard $3/$15 "sonnet" rate. Priced per the
# message's own send date, not today's, so historical costs stay correct once
# this window closes.
SONNET5_INTRO_END = date(2026, 8, 31)


def _claude_rate(model: str, on: date | None = None) -> tuple[float, float]:
    m = (model or "").lower()
    if "sonnet-5" in m and (on or date.today()) > SONNET5_INTRO_END:
        m = m.replace("sonnet-5", "sonnet")
    for frag, rate in CLAUDE_PRICES.items():
        if frag in m:
            return rate
    return CLAUDE_PRICES["opus"]  # sane default


def _local_day(iso: str) -> str | None:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone().date().isoformat()
    except (ValueError, AttributeError):
        return None


def _cos_project_dir() -> Path:
    """The Claude Code transcript folder for CoS's Agent SDK calls. CoS runs the
    SDK from the backend CWD, and Claude Code names project folders by that path
    with '/' -> '-'. This folder is exclusively CoS (no interactive sessions)."""
    from .config import REPO_ROOT
    backend = (Path(REPO_ROOT) / "backend").resolve()
    name = str(backend).replace("/", "-")
    return Path.home() / ".claude" / "projects" / name


def _claude_usage(since: date, only: Path | None = None) -> dict[str, dict]:
    """Per-day {cost, input, output, cache} from Claude Code transcripts.
    only: restrict to a single project folder (used for CoS's own spend)."""
    root = Path.home() / ".claude" / "projects"
    by_day: dict[str, dict] = {}
    cutoff = datetime.combine(since, datetime.min.time()).timestamp()
    files = (only.glob("*.jsonl") if only else root.rglob("*.jsonl"))
    for f in files:
        try:
            if f.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        try:
            for line in f.open(errors="ignore"):
                if '"usage"' not in line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                msg = o.get("message")
                if not isinstance(msg, dict):
                    continue
                u = msg.get("usage")
                if not isinstance(u, dict):
                    continue
                day = _local_day(o.get("timestamp", ""))
                if not day or day < since.isoformat():
                    continue
                in_rate, out_rate = _claude_rate(msg.get("model", ""), on=date.fromisoformat(day))
                inp = u.get("input_tokens", 0) or 0
                out = u.get("output_tokens", 0) or 0
                cr = u.get("cache_read_input_tokens", 0) or 0
                cc = u.get("cache_creation", {}) or {}
                c5 = cc.get("ephemeral_5m_input_tokens", 0) or 0
                c1 = cc.get("ephemeral_1h_input_tokens", 0) or 0
                if not (c5 or c1):  # fall back to flat field
                    c5 = u.get("cache_creation_input_tokens", 0) or 0
                cost = (inp * in_rate + out * out_rate
                        + cr * in_rate * CACHE_READ_MULT
                        + c5 * in_rate * CACHE_WRITE_5M
                        + c1 * in_rate * CACHE_WRITE_1H)
                d = by_day.setdefault(day, {"cost": 0.0, "input": 0, "output": 0, "cache_read": 0})
                d["cost"] += cost
                d["input"] += inp
                d["output"] += out
                d["cache_read"] += cr
        except OSError:
            continue
    return by_day


def _codex_usage(since: date) -> dict[str, dict]:
    """Per-day Codex usage. total_token_usage is cumulative per session, so we
    take the last occurrence per file and attribute it to the file's day."""
    root = Path.home() / ".codex" / "sessions"
    prices = policy().get("cost", {}).get("openai_prices", {})  # {fragment: [in_per_mtok, out_per_mtok]}
    by_day: dict[str, dict] = {}
    cutoff = datetime.combine(since, datetime.min.time()).timestamp()
    if not root.exists():
        return by_day
    for f in root.rglob("*.jsonl"):
        try:
            st = f.stat()
            if st.st_mtime < cutoff:
                continue
        except OSError:
            continue
        day = date.fromtimestamp(st.st_mtime).isoformat()
        if day < since.isoformat():
            continue
        last_usage, model = None, ""
        try:
            for line in f.open(errors="ignore"):
                if '"total_token_usage"' in line:
                    try:
                        o = json.loads(line)
                    except ValueError:
                        continue
                    found = _find_key(o, "total_token_usage")
                    if found:
                        last_usage = found
                if '"model"' in line and not model:
                    try:
                        model = json.loads(line).get("model") or _find_key(json.loads(line), "model") or ""
                    except (ValueError, AttributeError):
                        pass
        except OSError:
            continue
        if not last_usage:
            continue
        inp = last_usage.get("input_tokens", 0) or 0
        out = last_usage.get("output_tokens", 0) or 0
        rate = next((v for k, v in prices.items() if k.lower() in (model or "").lower()), None)
        cost = (inp * rate[0] / 1e6 + out * rate[1] / 1e6) if rate else None
        d = by_day.setdefault(day, {"cost": 0.0, "input": 0, "output": 0, "rates_missing": False})
        d["input"] += inp
        d["output"] += out
        if cost is None:
            d["rates_missing"] = True
        else:
            d["cost"] += cost
    return by_day


def _find_key(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = _find_key(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_key(v, key)
            if r is not None:
                return r
    return None


def record_run(label: str, model: str, cost_usd, usage: dict | None, *,
               duration_ms: int | None = None, ok: bool = True,
               error: str | None = None) -> None:
    """Meter one CoS agent run (run_agent) into the cos_cost table. CoS's own
    operating cost = the sum of these, separate from total Claude Code CLI usage.
    Failures are metered too (duration/ok/error) so the Health tab can show
    agent error rates. Never raises: metering must not break an agent call."""
    try:
        from . import db
        u = usage or {}
        db.execute(
            "INSERT INTO cos_cost(ts,label,model,cost_usd,input_tokens,output_tokens,"
            "duration_ms,ok,error) VALUES(?,?,?,?,?,?,?,?,?)",
            (db.now(), label or "agent", model or "",
             float(cost_usd or 0.0),
             int(u.get("input_tokens", 0) or 0), int(u.get("output_tokens", 0) or 0),
             duration_ms, 1 if ok else 0, str(error)[:500] if error else None))
    except Exception:
        log.exception("cos cost metering failed")


def cos_summary() -> dict:
    """What CoS itself costs to run. Totals come from CoS's Claude Code
    transcripts (covers all past days, not just since metering started); the
    per-purpose breakdown comes from the metered cos_cost table (forward-only)."""
    from . import db
    today = date.today()
    month_start = today.replace(day=1)
    t = today.isoformat()

    by_day = _claude_usage(month_start, only=_cos_project_dir())

    def agg(days):
        return {"cost": round(sum(by_day.get(d, {}).get("cost", 0.0) for d in days), 4),
                "input": sum(by_day.get(d, {}).get("input", 0) for d in days),
                "output": sum(by_day.get(d, {}).get("output", 0) for d in days)}

    # labeled breakdown (best-effort; only since per-call metering began)
    label_rows = db.query(
        "SELECT label, SUM(cost_usd) c, COUNT(*) n FROM cos_cost WHERE ts >= ? GROUP BY label",
        (month_start.isoformat(),))
    by_label = sorted(
        ({"label": r["label"], "cost": round(r["c"] or 0, 4), "calls": r["n"]}
         for r in label_rows), key=lambda x: x["cost"], reverse=True)

    return {
        "today": agg([t]),
        "month": agg(set(by_day)),
        "by_label": by_label,
        "days": len(by_day),
        "month_label": today.strftime("%b %Y"),
    }


def _month_bounds(month: str | None) -> tuple[date, date]:
    """(start, next_start) for a 'YYYY-MM' string; defaults to the current month.
    Falls back to the current month on any parse error."""
    today = date.today()
    start = today.replace(day=1)
    if month:
        try:
            y, m = (int(x) for x in month.split("-")[:2])
            start = date(y, m, 1)
        except (ValueError, TypeError):
            pass
    nxt = date(start.year + 1, 1, 1) if start.month == 12 else date(start.year, start.month + 1, 1)
    return start, nxt


def _cos_available_months() -> list[str]:
    """'YYYY-MM' months that have any CoS activity, newest first: from the earliest
    Claude Code transcript and the earliest metered row, through the current month."""
    from . import db
    today = date.today()
    earliest = today.replace(day=1)
    proj = _cos_project_dir()
    if proj.exists():
        mtimes = [f.stat().st_mtime for f in proj.glob("*.jsonl")]
        if mtimes:
            earliest = min(earliest, date.fromtimestamp(min(mtimes)).replace(day=1))
    row = db.query("SELECT MIN(ts) m FROM cos_cost")
    if row and row[0]["m"]:
        d = _local_day(row[0]["m"])
        if d:
            earliest = min(earliest, date.fromisoformat(d).replace(day=1))
    months, cur = [], earliest
    end = today.replace(day=1)
    while cur <= end:
        months.append(cur.strftime("%Y-%m"))
        cur = date(cur.year + 1, 1, 1) if cur.month == 12 else date(cur.year, cur.month + 1, 1)
    return list(reversed(months))


def _esc(s) -> str:
    return html.escape(str(s or ""), quote=True)


def _week_key(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def _weekly_from_daily(daily: list[dict]) -> list[dict]:
    """Bucket a daily cost list into ISO weeks. Weeks at a month boundary are
    partial (only the days actually in `daily`) since `daily` is month-scoped."""
    buckets: dict[str, dict] = {}
    for d in daily:
        day = date.fromisoformat(d["day"])
        monday = day - timedelta(days=day.weekday())
        key = _week_key(day)
        b = buckets.setdefault(key, {
            "week": key, "label": f"Week of {monday.strftime('%b %d')}",
            "cost": 0.0, "input": 0, "output": 0, "days": 0})
        b["cost"] += d["cost"]
        b["input"] += d["input"]
        b["output"] += d["output"]
        b["days"] += 1
    for b in buckets.values():
        b["cost"] = round(b["cost"], 4)
    return sorted(buckets.values(), key=lambda x: x["week"])


def cos_detail(month: str | None = None) -> dict:
    """Full CoS cost breakdown for the Cost tab: daily trend (from CoS's Claude
    Code logs), plus by-purpose and by-model splits (metered table). Scoped to
    `month` ('YYYY-MM'); defaults to the current month."""
    from . import db
    today = date.today()
    month_start, next_start = _month_bounds(month)
    prefix = month_start.strftime("%Y-%m")
    is_current = prefix == today.strftime("%Y-%m")

    by_day = {d: v for d, v in _claude_usage(month_start, only=_cos_project_dir()).items()
              if d.startswith(prefix)}
    daily = sorted(
        ({"day": d, "cost": round(v.get("cost", 0.0), 4),
          "input": v.get("input", 0), "output": v.get("output", 0)}
         for d, v in by_day.items()), key=lambda x: x["day"])
    month_cost = round(sum(d["cost"] for d in daily), 4)
    today_cost = round(by_day.get(today.isoformat(), {}).get("cost", 0.0), 4) if is_current else 0.0

    span = (month_start.isoformat(), next_start.isoformat())
    lr = db.query(
        "SELECT label, SUM(cost_usd) c, COUNT(*) n, SUM(input_tokens) i, "
        "SUM(output_tokens) o FROM cos_cost WHERE ts >= ? AND ts < ? GROUP BY label", span)
    by_label = sorted(({"label": r["label"], "cost": round(r["c"] or 0, 4),
                        "calls": r["n"], "input": r["i"] or 0, "output": r["o"] or 0}
                       for r in lr), key=lambda x: x["cost"], reverse=True)
    mr = db.query(
        "SELECT model, SUM(cost_usd) c, COUNT(*) n FROM cos_cost "
        "WHERE ts >= ? AND ts < ? GROUP BY model", span)
    by_model = sorted(({"model": r["model"], "cost": round(r["c"] or 0, 4), "calls": r["n"]}
                       for r in mr), key=lambda x: x["cost"], reverse=True)
    metered = round(sum(x["cost"] for x in by_label), 4)

    return {
        "today": today_cost, "month": month_cost,
        "days": len(daily), "month_label": month_start.strftime("%b %Y"),
        "month_key": prefix, "is_current": is_current,
        "months": _cos_available_months(),
        "daily": daily, "weekly": _weekly_from_daily(daily),
        "by_label": by_label, "by_model": by_model,
        "metered_total": metered,  # breakdown only covers spend since metering began
    }


def summary() -> dict:
    today = date.today()
    month_start = today.replace(day=1)
    claude = _claude_usage(month_start)
    codex = _codex_usage(month_start)

    def agg(by_day, days):
        return {
            "cost": round(sum(by_day.get(d, {}).get("cost", 0.0) for d in days), 2),
            "input": sum(by_day.get(d, {}).get("input", 0) for d in days),
            "output": sum(by_day.get(d, {}).get("output", 0) for d in days),
        }

    month_days = [d for d in claude] + [d for d in codex]
    t = today.isoformat()
    return {
        "claude": {
            "today": agg(claude, [t]),
            "month": agg(claude, set(claude)),
        },
        "codex": {
            "today": agg(codex, [t]),
            "month": agg(codex, set(codex)),
            "rates_missing": any(v.get("rates_missing") for v in codex.values()),
        },
        "month_label": today.strftime("%b %Y"),
        "scope": "CLI usage on this Mac",
    }


# ---------------------------------------------------------------------------
# Reports & receipts — printable/exportable snapshots of cos_detail()
# ---------------------------------------------------------------------------

LABEL_NAMES = {
    "triage": "Triage", "pr_review": "PR review", "weekly_status": "Weekly status",
    "roadmap": "Roadmap", "morning_brief": "Morning brief", "chieff": "@CoS replies",
    "pre_meeting_brief": "Pre-meeting brief", "draft_retry": "Draft retry",
    "agent": "Other",
}


def render_cost_report_html(d: dict) -> str:
    """Standalone LIGHT print HTML: a full cost report for one month — headline
    numbers, weekly/daily trend, and the by-purpose/by-model breakdown. Mirrors
    the light-print style used by roadmap.py's PDF exports."""
    generated = datetime.now().strftime("%b %d, %Y %H:%M")
    avg_day = d["month"] / d["days"] if d.get("days") else 0.0

    def stat(label, value):
        return (f'<div class="stat"><div class="stat-label">{_esc(label)}</div>'
                f'<div class="stat-value">{_esc(value)}</div></div>')

    p = ['<!doctype html><html><head><meta charset="utf-8"><style>',
         "*{box-sizing:border-box}"
         "body{font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;color:#172B4D;margin:0;font-size:11px;}"
         ".hdr{background:#1e2a4a;color:#fff;display:flex;justify-content:space-between;align-items:center;padding:12px 16px;border-radius:6px;}"
         ".hdr h1{margin:0;font-size:17px;} .hdr .meta{text-align:right;font-size:10px;color:#c7d0e0;}"
         ".stats{display:flex;gap:10px;margin:12px 0;}"
         ".stat{flex:1;border:1px solid #dfe1e6;border-radius:8px;padding:8px 10px;}"
         ".stat-label{font-size:9px;text-transform:uppercase;letter-spacing:.04em;color:#6b7280;}"
         ".stat-value{font-size:15px;font-weight:700;color:#172B4D;margin-top:2px;}"
         "h2{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:#1e3a8a;"
         "border-bottom:1px solid #c7ced9;padding-bottom:3px;margin:16px 0 6px;}"
         "table{width:100%;border-collapse:collapse;font-size:10.5px;}"
         "th{text-align:left;color:#6b7280;font-weight:600;padding:3px 6px;border-bottom:1px solid #dfe1e6;}"
         "td{padding:3px 6px;border-bottom:1px solid #eef0f3;}"
         "td.num,th.num{text-align:right;tabular-nums:1;font-variant-numeric:tabular-nums;}"
         ".foot{margin-top:16px;padding-top:8px;border-top:1px solid #dfe1e6;font-size:9.5px;"
         "color:#6b7280;font-style:italic;}"
         "</style></head><body>"]
    p.append(f'<div class="hdr"><h1>Chief of Staff — Cost Report</h1>'
             f'<div class="meta">{_esc(d["month_label"])}<br>Generated {_esc(generated)}</div></div>')
    p.append('<div class="stats">')
    p.append(stat("Month total", f'${d["month"]:.2f}'))
    if d.get("is_current"):
        p.append(stat("Today", f'${d["today"]:.2f}'))
    p.append(stat("Active days", str(d.get("days", 0))))
    p.append(stat("Avg / active day", f'${avg_day:.2f}'))
    p.append('</div>')

    if d.get("weekly"):
        p.append('<h2>By week</h2><table><tr><th>Week</th><th class="num">Days active</th>'
                 '<th class="num">Cost</th></tr>')
        for w in d["weekly"]:
            p.append(f'<tr><td>{_esc(w["label"])}</td><td class="num">{w["days"]}</td>'
                     f'<td class="num">${w["cost"]:.2f}</td></tr>')
        p.append('</table>')

    if d.get("daily"):
        p.append('<h2>By day</h2><table><tr><th>Day</th><th class="num">Input tok</th>'
                 '<th class="num">Output tok</th><th class="num">Cost</th></tr>')
        for x in d["daily"]:
            p.append(f'<tr><td>{_esc(x["day"])}</td><td class="num">{x["input"]:,}</td>'
                     f'<td class="num">{x["output"]:,}</td><td class="num">${x["cost"]:.4f}</td></tr>')
        p.append('</table>')

    if d.get("by_label"):
        p.append('<h2>By purpose (metered)</h2><table><tr><th>Purpose</th>'
                 '<th class="num">Calls</th><th class="num">Cost</th></tr>')
        for b in d["by_label"]:
            name = LABEL_NAMES.get(b["label"], b["label"])
            p.append(f'<tr><td>{_esc(name)}</td><td class="num">{b["calls"]}</td>'
                     f'<td class="num">${b["cost"]:.4f}</td></tr>')
        p.append('</table>')

    if d.get("by_model"):
        p.append('<h2>By model (metered)</h2><table><tr><th>Model</th>'
                 '<th class="num">Calls</th><th class="num">Cost</th></tr>')
        for m in d["by_model"]:
            p.append(f'<tr><td>{_esc(m["model"])}</td><td class="num">{m["calls"]}</td>'
                     f'<td class="num">${m["cost"]:.4f}</td></tr>')
        p.append('</table>')

    p.append('<div class="foot">Scope: CLI usage on this Mac (Claude Code transcripts + '
             'per-call metering since tracking began) — not a billing-accurate org figure. '
             'Generated by Chief of Staff.</div>')
    p.append('</body></html>')
    return "".join(p)


async def cos_report_pdf(d: dict) -> bytes:
    """Cost report -> portrait A4 PDF, via the same headless-Chromium helper
    roadmap.py's PDF exports use."""
    from .roadmap import _html_to_pdf
    return await _html_to_pdf(render_cost_report_html(d), fmt="A4", landscape=False)


def cos_report_csv(d: dict) -> str:
    """Cost report -> CSV: one section per breakdown, for spreadsheet import
    or as a plain-text receipt."""
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Chief of Staff — Cost Report", d["month_label"]])
    w.writerow([])
    w.writerow(["Month total", f'{d["month"]:.4f}'])
    w.writerow(["Active days", d.get("days", 0)])
    w.writerow(["Avg / active day", f'{(d["month"] / d["days"] if d.get("days") else 0):.4f}'])
    w.writerow([])
    w.writerow(["Week", "Days active", "Cost"])
    for wk in d.get("weekly", []):
        w.writerow([wk["label"], wk["days"], f'{wk["cost"]:.4f}'])
    w.writerow([])
    w.writerow(["Day", "Input tokens", "Output tokens", "Cost"])
    for x in d.get("daily", []):
        w.writerow([x["day"], x["input"], x["output"], f'{x["cost"]:.4f}'])
    w.writerow([])
    w.writerow(["Purpose", "Calls", "Cost"])
    for b in d.get("by_label", []):
        w.writerow([LABEL_NAMES.get(b["label"], b["label"]), b["calls"], f'{b["cost"]:.4f}'])
    w.writerow([])
    w.writerow(["Model", "Calls", "Cost"])
    for m in d.get("by_model", []):
        w.writerow([m["model"], m["calls"], f'{m["cost"]:.4f}'])
    return buf.getvalue()
