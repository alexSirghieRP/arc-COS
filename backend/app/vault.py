"""Obsidian vault: daily note location, parsing, checkbox sync, appends.

Daily notes: <vault>/To Do/Daily/<YYYY>-W<ww>/<YYYY-MM-DD dddd>.md
The note is the source of truth; this module only reads and makes surgical edits.
"""

import re
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import policy, vault_root

SECTIONS = ["Top 3 for Today", "Also Do Today", "Notes / Decisions Today", "Carry Forward"]
CHECKBOX_RE = re.compile(r"^(\s*)- \[( |x|X)\] ?(.*)$")


def daily_dir() -> Path:
    return vault_root() / policy()["vault"]["daily_dir"]


def note_path(d: date) -> Path:
    iso = d.isocalendar()
    week_dir = f"{iso.year}-W{iso.week:02d}"
    return daily_dir() / week_dir / f"{d.isoformat()} {d.strftime('%A')}.md"


def template_text() -> str:
    return (vault_root() / policy()["vault"]["template"]).read_text()


def most_recent_note(before: date | None = None, lookback_days: int = 60) -> tuple[date, Path] | None:
    """Walk backwards day by day; tolerates gaps."""
    d = before or date.today()
    for _ in range(lookback_days):
        p = note_path(d)
        if p.exists():
            return d, p
        d -= timedelta(days=1)
    return None


def parse_note(text: str) -> dict:
    """Split into sections; within each, extract checkboxes with line numbers."""
    lines = text.splitlines()
    sections: dict[str, dict] = {}
    current = None
    for i, line in enumerate(lines):
        m = re.match(r"^##\s+(.*)$", line)
        if m:
            current = m.group(1).strip()
            sections[current] = {"start": i, "lines": [], "checkboxes": []}
            continue
        if current is None:
            continue
        sections[current]["lines"].append(line)
        cb = CHECKBOX_RE.match(line)
        if cb and cb.group(3).strip():
            sections[current]["checkboxes"].append({
                "line": i,
                "checked": cb.group(2).lower() == "x",
                "text": cb.group(3).strip(),
            })
    return sections


def read_note(d: date) -> dict | None:
    p = note_path(d)
    if not p.exists():
        return None
    text = p.read_text()
    sections = parse_note(text)
    return {
        "date": d.isoformat(),
        "path": str(p),
        "sections": {
            name: {"checkboxes": s["checkboxes"], "text": "\n".join(s["lines"]).strip()}
            for name, s in sections.items()
        },
    }


def toggle_checkbox(d: date, line_no: int, checked: bool) -> bool:
    """Flip one checkbox by line number. Returns False if the line moved."""
    p = note_path(d)
    lines = p.read_text().splitlines(keepends=True)
    if line_no >= len(lines):
        return False
    m = CHECKBOX_RE.match(lines[line_no].rstrip("\n"))
    if not m:
        return False
    mark = "x" if checked else " "
    lines[line_no] = f"{m.group(1)}- [{mark}] {m.group(3)}\n"
    p.write_text("".join(lines))
    return True


def append_to_section(d: date, section: str, content: str) -> bool:
    """Append content at the end of a section (before the next ## or EOF)."""
    p = note_path(d)
    if not p.exists():
        return False
    lines = p.read_text().splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(rf"^##\s+{re.escape(section)}\s*$", line):
            start = i
            break
    if start is None:
        return False
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## ") or lines[i].strip() == "---":
            end = i
            break
    # trim trailing blank lines inside the section, then insert
    while end > start + 1 and lines[end - 1].strip() == "":
        end -= 1
    insert = content.rstrip("\n").splitlines()
    lines[end:end] = insert + [""]
    p.write_text("\n".join(lines) + "\n")
    return True


def create_note(d: date, body: str | None = None) -> Path:
    p = note_path(d)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        text = body if body is not None else template_text().replace(
            "{{date}}", f"{d.isoformat()} {d.strftime('%A')}"
        )
        p.write_text(text)
    return p


def unchecked_items(d: date) -> list[str]:
    """All unchecked checkbox texts from a note, across task sections."""
    note = read_note(d)
    if not note:
        return []
    out = []
    for sec in ("Top 3 for Today", "Also Do Today", "Carry Forward"):
        for cb in note["sections"].get(sec, {}).get("checkboxes", []):
            if not cb["checked"]:
                out.append(cb["text"])
    return out


# ---- Chieff's zone ----------------------------------------------------------
# Chieff/ is the agent's own space: logs, notes, thinking. Chieff may also
# write into the user's zone, but every such write must leave a trace: the 🤖
# marker inline plus an entry in Chieff's daily log.

# One folder per connector/domain Chieff acts through. Every action writes to
# the master Log plus its domain folder.
CHIEFF_DOMAINS = ["Teams", "Email", "Calendar", "Meetings", "GitHub",
                  "AzureDevOps", "Confluence", "Salesforce"]


def chieff_dir() -> Path:
    return vault_root() / policy()["vault"].get("chieff_dir", "Chieff")


def ensure_chieff_dirs() -> None:
    for sub in ["Log", "Notes", *CHIEFF_DOMAINS]:
        (chieff_dir() / sub).mkdir(parents=True, exist_ok=True)


def _chieff_append(folder: str, line: str) -> Path:
    d = date.today()
    p = chieff_dir() / folder / f"{d.isoformat()}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        title = "Chieff log" if folder == "Log" else f"Chieff / {folder}"
        p.write_text(f"# {title} {d.isoformat()}\n\n")
    with p.open("a") as f:
        f.write(line.rstrip("\n") + "\n")
    return p


def chieff_trace(domain: str | None, content: str) -> None:
    """Best-effort trace of a Chieff action: master Log always, plus the
    domain's folder when given. Never raises; tracing must not break sends."""
    try:
        stamp = datetime.now().astimezone().strftime("%H:%M")
        line = f"- {stamp} {content.strip()}"
        _chieff_append("Log", line)
        if domain:
            _chieff_append(domain, line)
    except Exception:
        import logging
        logging.getLogger("chief.vault").exception("chieff trace failed")


def chieff_log(content: str) -> Path:
    """Append to Chieff's daily master log (legacy raw-line variant)."""
    return _chieff_append("Log", content)


def append_as_chieff(d: date, section: str, content: str) -> bool:
    """Chieff writing into the user's daily note: tag every line with the 🤖
    marker and mirror the edit into Chieff's log so there is always a trace."""
    tagged = "\n".join(
        line if line.strip().startswith("🤖") or not line.strip() else f"{line} 🤖"
        for line in content.rstrip("\n").splitlines())
    ok = append_to_section(d, section, tagged)
    where = f"{d.isoformat()} / {section}" if ok else f"{d.isoformat()} / {section} (FAILED: section missing)"
    chieff_trace(None, f"edited the user's note [{where}]: {content.strip()[:160]}")
    return ok
