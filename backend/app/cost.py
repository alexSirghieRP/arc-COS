"""LLM usage cost tracking from local CLI logs.

Claude: per-message token usage from ~/.claude/projects/**/*.jsonl, priced at
current Anthropic rates (verified via the claude-api reference, 2026-06).
Codex: cumulative per-session usage from ~/.codex/sessions/**/*.jsonl; OpenAI
rates are configurable (cost.openai_prices in policy.yaml) since they aren't
hardcoded — tokens always show, cost shows once rates are set.

Scope: this is CLI usage on THIS machine, the best signal available without an
Anthropic Admin / OpenAI billing API key. Not a billing-accurate org figure.
"""

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path

from .config import policy

log = logging.getLogger("chief.cost")

# Anthropic price per token (input, output), by model-name fragment.
CLAUDE_PRICES = {
    "fable": (10/1e6, 50/1e6), "mythos": (10/1e6, 50/1e6),
    "opus": (5/1e6, 25/1e6), "sonnet": (3/1e6, 15/1e6), "haiku": (1/1e6, 5/1e6),
}
CACHE_READ_MULT = 0.1
CACHE_WRITE_5M = 1.25
CACHE_WRITE_1H = 2.0


def _claude_rate(model: str) -> tuple[float, float]:
    m = (model or "").lower()
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
                in_rate, out_rate = _claude_rate(msg.get("model", ""))
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


def record_run(label: str, model: str, cost_usd, usage: dict | None) -> None:
    """Meter one CoS agent run (run_agent) into the cos_cost table. CoS's own
    operating cost = the sum of these, separate from total Claude Code CLI usage.
    Never raises: metering must not break an agent call."""
    try:
        from . import db
        u = usage or {}
        db.execute(
            "INSERT INTO cos_cost(ts,label,model,cost_usd,input_tokens,output_tokens) "
            "VALUES(?,?,?,?,?,?)",
            (db.now(), label or "agent", model or "",
             float(cost_usd or 0.0),
             int(u.get("input_tokens", 0) or 0), int(u.get("output_tokens", 0) or 0)))
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


def cos_detail() -> dict:
    """Full CoS cost breakdown for the Cost tab: daily trend (all history from
    CoS's Claude Code logs), plus by-purpose and by-model splits (metered table)."""
    from . import db
    today = date.today()
    month_start = today.replace(day=1)

    by_day = _claude_usage(month_start, only=_cos_project_dir())
    daily = sorted(
        ({"day": d, "cost": round(v.get("cost", 0.0), 4),
          "input": v.get("input", 0), "output": v.get("output", 0)}
         for d, v in by_day.items()), key=lambda x: x["day"])
    month_cost = round(sum(d["cost"] for d in daily), 4)
    today_cost = round(by_day.get(today.isoformat(), {}).get("cost", 0.0), 4)

    lr = db.query(
        "SELECT label, SUM(cost_usd) c, COUNT(*) n, SUM(input_tokens) i, "
        "SUM(output_tokens) o FROM cos_cost WHERE ts >= ? GROUP BY label",
        (month_start.isoformat(),))
    by_label = sorted(({"label": r["label"], "cost": round(r["c"] or 0, 4),
                        "calls": r["n"], "input": r["i"] or 0, "output": r["o"] or 0}
                       for r in lr), key=lambda x: x["cost"], reverse=True)
    mr = db.query(
        "SELECT model, SUM(cost_usd) c, COUNT(*) n FROM cos_cost WHERE ts >= ? GROUP BY model",
        (month_start.isoformat(),))
    by_model = sorted(({"model": r["model"], "cost": round(r["c"] or 0, 4), "calls": r["n"]}
                       for r in mr), key=lambda x: x["cost"], reverse=True)
    metered = round(sum(x["cost"] for x in by_label), 4)

    return {
        "today": today_cost, "month": month_cost,
        "days": len(daily), "month_label": today.strftime("%b %Y"),
        "daily": daily, "by_label": by_label, "by_model": by_model,
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
