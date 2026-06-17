"""Open-loops reconciliation: rebuild the set of aged things awaiting movement.

Loops live in items as type='open_loop' with subject=kind. Each sweep upserts
the currently-true loops and closes the ones that no longer hold.
"""

import logging
from datetime import date, datetime, timedelta, timezone

from .. import db, state, vault
from ..config import policy

log = logging.getLogger("chief.sweep.loops")


def _loop(kind: str, key: str, content: str, since: str | None):
    item_id, is_new = db.upsert_item(
        source="loops", type_="open_loop", external_id=f"{kind}:{key}",
        subject=kind, content=content, received_at=since or db.now(),
    )
    if is_new:
        db.audit("loop_opened", {"kind": kind, "content": content[:200]}, item_id=item_id)
    else:
        db.update_item(item_id, content=content, status="classified")
    return f"{kind}:{key}"


async def sweep() -> dict:
    cfg = policy()["open_loops"]
    seen: set[str] = set()

    # 1. Emails that asked the user something and got no reply in N+ days
    email_days = cfg.get("email_unanswered_days", 2)
    rows = db.query(
        "SELECT * FROM items WHERE source='email' AND tier IN ('B','C') "
        "AND status IN ('classified','drafted') AND received_at < ?",
        (db.cutoff(days=int(email_days)),))
    for r in rows:
        seen.add(_loop("email_unanswered", r["external_id"][:40],
                       f"{r['sender']}: {r['subject']}", r["received_at"]))

    # 2. Teams mentions the user went silent on (no reply, 1+ day old)
    rows = db.query(
        "SELECT * FROM items WHERE source='teams_chat' AND type='mention' "
        "AND status IN ('classified','drafted','held') AND received_at < ?",
        (db.cutoff(days=1),))
    for r in rows:
        seen.add(_loop("silent_mention", r["external_id"][:40],
                       f"{r['sender']} in {r['subject']}: {(r['content'] or '')[:120]}",
                       r["received_at"]))

    # 3 + 4. PRs awaiting my review / my PRs stuck awaiting others
    for pr in await state._gh_prs(["--review-requested=@me"]):
        seen.add(_loop("pr_awaiting_me", pr["url"],
                       f"{pr['repository']['nameWithOwner']}: {pr['title']}", pr["updatedAt"]))
    for pr in await state._gh_prs(["--author=@me"]):
        updated = datetime.fromisoformat(pr["updatedAt"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) - updated > timedelta(days=2):
            seen.add(_loop("my_pr_stuck", pr["url"],
                           f"{pr['repository']['nameWithOwner']}: {pr['title']}", pr["updatedAt"]))

    # 5. Checkboxes carried unchecked across N+ days of notes
    carry_days = cfg.get("checkbox_carry_days", 3)
    recent = vault.most_recent_note()
    if recent:
        latest_date, _ = recent
        latest_unchecked = set(vault.unchecked_items(latest_date))
        if latest_unchecked:
            first_seen: dict[str, date] = {}
            d = latest_date - timedelta(days=1)
            for _ in range(14):
                if vault.note_path(d).exists():
                    for text in vault.unchecked_items(d):
                        if text in latest_unchecked:
                            first_seen[text] = d
                d -= timedelta(days=1)
            for text, since in first_seen.items():
                if (latest_date - since).days >= carry_days:
                    seen.add(_loop("carried_checkbox", text[:60],
                                   f"unchecked since {since.isoformat()}: {text}",
                                   since.isoformat() + "T00:00:00+00:00"))

    # Close loops that no longer hold
    open_now = db.query("SELECT id, external_id FROM items WHERE type='open_loop' AND status != 'done'")
    closed = 0
    for r in open_now:
        if r["external_id"] not in seen:
            db.update_item(r["id"], status="done", action_taken="loop closed")
            db.audit("loop_closed", {"loop": r["external_id"]}, item_id=r["id"])
            closed += 1

    log.info("open loops: %d open, %d closed", len(seen), closed)
    return {"open": len(seen), "closed": closed}


# ---- "Run now" from the board: act on selected loops -----------------------

FOLLOWUP_SCHEMA = {
    "type": "object",
    "properties": {"draft": {"type": "string"}},
    "required": ["draft"],
}

FOLLOWUP_PROMPT = """the user has not replied to this for a while. Draft a short follow-up reply
on his behalf, for his approval (it will NOT be sent automatically). Friendly, brief,
plain language, moves the thread forward or politely buys time honestly. Never use em dashes.

Channel: {channel}
From: {sender}
Context: {content}
"""


async def _run_pr_review_loop(loop: dict) -> str:
    from .pr_review import _find_chat_id, _review_one
    from .. import graph
    from ..config import policy as _policy
    url = loop["external_id"].split(":", 1)[1]
    cfg = _policy().get("pr_review", {})
    topic = cfg.get("chat_topic")
    chat_id = _find_chat_id(await graph.list_chats(top=50), topic) if topic else None
    if not chat_id:
        return "pr_review chat not found; cannot post review"
    item_id, is_new = db.upsert_item(
        source="pr_review", type_="pr_review", external_id=url.lower(),
        conversation_id=chat_id, subject=topic, content=url)
    if not is_new:
        rows = db.query("SELECT status FROM items WHERE id=?", (item_id,))
        if rows and rows[0]["status"] == "done":
            return "already reviewed earlier"
    await _review_one(url, chat_id, item_id, cfg)
    return "reviewed; findings posted"


async def _nudge_stuck_pr(loop: dict) -> str:
    from .. import actions
    from datetime import datetime, timezone
    url = loop["external_id"].split(":", 1)[1]
    idle = (datetime.now(timezone.utc)
            - datetime.fromisoformat(loop["received_at"].replace("Z", "+00:00"))).days
    emoji = policy().get("chieff", {}).get("emoji", "🤖")
    body = (f"{emoji} Friendly nudge from the user's assistant: this PR has been waiting on "
            f"review for {idle} day(s). A look would be much appreciated.")
    return f"nudge comment: {await actions.post_github_comment(url, body, item_id=loop['id'])}"


async def _draft_followup(loop: dict, kind: str) -> str:
    from ..agents import CHIEF, run_agent
    prefix = loop["external_id"].split(":", 1)[1]
    source = "email" if kind == "email_unanswered" else "teams_chat"
    rows = db.query(
        "SELECT * FROM items WHERE source=? AND external_id LIKE ? "
        "ORDER BY received_at DESC LIMIT 1", (source, prefix + "%"))
    if not rows:
        return "original item not found"
    orig = rows[0]
    pending = db.query(
        "SELECT 1 FROM drafts WHERE item_id=? AND status='pending'", (orig["id"],))
    if pending:
        return "draft already awaiting approval"
    out = await run_agent(
        CHIEF,
        FOLLOWUP_PROMPT.format(
            channel="email" if source == "email" else "teams",
            sender=orig["sender"] or "unknown",
            content=(orig["content"] or orig["subject"] or "")[:1500]),
        schema=FOLLOWUP_SCHEMA, with_tools=False, max_turns=4)
    channel = "email" if source == "email" else "teams"
    reply_ref = orig["external_id"] if channel == "email" else orig["conversation_id"]
    draft_id = db.execute(
        "INSERT INTO drafts(item_id, channel, recipient, reply_to_ref, body, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (orig["id"], channel, orig["sender"], reply_ref, out["draft"], db.now()))
    db.update_item(orig["id"], status="drafted", action_taken="follow-up draft from loop run")
    db.audit("draft_created", {"draft_id": draft_id, "body": out["draft"],
                               "conversation_id": reply_ref}, item_id=orig["id"])
    return "follow-up draft created for approval"


async def run_selected(ids: list[int]) -> dict:
    """Act on chosen open loops from the board. Every outbound step goes
    through actions.py, so kill switch, dry run and allowlists all apply."""
    from .. import actions
    results = []
    for item_id in ids:
        rows = db.query("SELECT * FROM items WHERE id=? AND type='open_loop'", (item_id,))
        if not rows:
            results.append({"id": item_id, "kind": None, "result": "not found"})
            continue
        loop = rows[0]
        kind = loop["subject"]
        try:
            if kind == "pr_awaiting_me":
                res = await _run_pr_review_loop(loop)
            elif kind == "my_pr_stuck":
                res = await _nudge_stuck_pr(loop)
            elif kind in ("email_unanswered", "silent_mention"):
                res = await _draft_followup(loop, kind)
            else:
                res = "needs the user (no automation for this loop kind)"
        except actions.SendBlocked as e:
            res = f"blocked: {e}"
        except Exception as e:
            log.exception("loop run failed for item %s", item_id)
            res = f"failed: {str(e)[:150]}"
        results.append({"id": item_id, "kind": kind, "result": res})
        db.audit("loop_run", {"kind": kind, "result": str(res)[:200]}, item_id=item_id)
        domain = {"pr_awaiting_me": "GitHub", "my_pr_stuck": "GitHub",
                  "email_unanswered": "Email", "silent_mention": "Teams"}.get(kind)
        vault.chieff_trace(domain, f"loop run [{kind}] {(loop['content'] or '')[:80]}: {res}")
    return {"results": results}
