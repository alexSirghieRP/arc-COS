"""Summarize a meeting from available sources into the daily note."""

import logging
from datetime import date

from fastapi import HTTPException

from . import db, graph, vault
from .agents import CHIEF, run_agent

log = logging.getLogger("chief.meetings")

SUMMARY_PROMPT = """Summarize this meeting for the user's daily note. 3 to 6 bullet lines:
decisions made, actions assigned (who/what), open questions. If the material is thin,
say what little is known in one line; do not invent content. Plain text bullets starting
with "- ". No em dashes, no headers.

Meeting: {subject} on {day}

Teams meeting chat messages (may be partial or empty):
{chat}

Existing notes from the user's vault for that day (may be unrelated):
{notes}
"""


async def summarize_into_note(day_s: str, subject: str, chat_id: str | None) -> dict:
    d = date.fromisoformat(day_s)
    if not vault.note_path(d).exists():
        raise HTTPException(409, f"no daily note for {day_s}; run morning brief/backfill first")

    chat_lines: list[str] = []
    if chat_id is None:
        chats = await graph.list_chats(top=50)
        for c in chats:
            if c.get("chatType") == "meeting" and (c.get("topic") or "").strip().lower() == subject.strip().lower():
                chat_id = c["id"]
                break
    if chat_id:
        try:
            for m in await graph.read_chat_messages(chat_id, top=40):
                user = ((m.get("from") or {}).get("user") or {}).get("displayName")
                text = graph.strip_html((m.get("body") or {}).get("content", ""))
                if user and text:
                    chat_lines.append(f"{user}: {text[:400]}")
        except Exception as e:
            log.warning("meeting chat read failed: %s", e)
    chat_lines.reverse()

    note = vault.read_note(d)
    notes_text = note["sections"].get("Notes / Decisions Today", {}).get("text", "") if note else ""

    summary = await run_agent(
        CHIEF,
        SUMMARY_PROMPT.format(subject=subject, day=day_s,
                              chat="\n".join(chat_lines[-60:]) or "(no chat messages found)",
                              notes=notes_text[:3000] or "(none)"),
        with_tools=False, max_turns=4)
    summary = (summary or "").strip()
    if not summary:
        raise HTTPException(502, "summarizer returned nothing")

    block = f"{subject} (summary by Chief):\n{summary}"
    vault.append_to_section(d, "Notes / Decisions Today", block)
    db.audit("note_written", {"date": day_s, "meeting": subject,
                              "sources": {"chat_messages": len(chat_lines)},
                              "summary": summary})
    return {"ok": True, "summary": summary, "chat_messages_used": len(chat_lines)}
