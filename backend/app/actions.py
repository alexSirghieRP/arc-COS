"""The ONLY path for outbound sends. Guardrails enforced here, in code:

- kill switch: blocks everything instantly
- dry run: logs and surfaces instead of sending
- signature appended to every message sent on the user's behalf
- em dashes stripped from anything outbound
- auto-sends: max 1 per conversation per 30 min, and only while the user is away
- email is never auto-sent; email sends require a human-approved draft
"""

import asyncio
import logging
import subprocess

from . import db, graph
from .config import policy

log = logging.getLogger("chief.actions")


class SendBlocked(Exception):
    pass


def _clean(body: str) -> str:
    body = body.replace("—", ", ").replace(" , ", ", ")
    return body.strip()


def _signed(body: str) -> str:
    sig = policy()["messaging"]["signature"]
    body = _clean(body)
    return body if body.endswith(sig) else f"{body}\n\n{sig}"


def _check_global_gates():
    if db.get_setting("kill_switch") == "true":
        raise SendBlocked("kill switch is on")


def board_write_overrides() -> tuple[set[str], set[str]]:
    """Board-managed (settings table) per-chat adds and removes.
    A remove beats every rule, including policy.yaml and categories."""
    import json as _json
    adds = set(_json.loads(db.get_setting("teams_write_add", "[]")))
    removes = set(_json.loads(db.get_setting("teams_write_remove", "[]")))
    return adds, removes


async def _teams_write_allowed(chat_id: str, sender: str | None = None) -> bool:
    """Writing is allowlist-only. Reading is unrestricted.

    Effective rule = (policy.yaml teams_write.allow + board adds) minus board
    removes. Entries match by exact chat_id, topic (group/meeting chats),
    person name (1:1 chats), or the meeting_chats category."""
    cfg = policy().get("teams_write")
    if cfg is None:
        return True  # no allowlist configured = no restriction
    adds, removes = board_write_overrides()
    if chat_id in removes:
        return False
    if chat_id in adds:
        return True
    allow = cfg.get("allow") or []
    chats = {c["id"]: c for c in await graph.chats_cached()}
    chat = chats.get(chat_id, {})
    topic = (chat.get("topic") or "").strip().lower()
    is_one_on_one = chat.get("chatType") == "oneOnOne"
    for entry in allow:
        if entry.get("chat_id") == chat_id:
            return True
        if entry.get("topic") and entry["topic"].strip().lower() == topic and topic:
            return True
        if (entry.get("person") and sender and is_one_on_one
                and entry["person"].strip().lower() == sender.strip().lower()):
            return True
        if entry.get("meeting_chats") and chat.get("chatType") == "meeting":
            return True
    return False


def _auto_send_allowed(conversation_id: str) -> bool:
    minutes = policy()["messaging"].get("rate_limit_minutes", 30)
    rows = db.query(
        "SELECT COUNT(*) AS n FROM audit_log "
        "WHERE action IN ('auto_reply','holding_message','chieff_auto') "
        "AND json_extract(detail, '$.conversation_id') = ? AND ts > ?",
        (conversation_id, db.cutoff(minutes=int(minutes))))
    return rows[0]["n"] == 0


async def auto_send_teams(chat_id: str, body: str, *, kind: str, item_id: int | None,
                          sender: str | None = None) -> str:
    """Agent-initiated Teams send (tier A auto-reply or holding message).

    Returns 'sent' | 'dry_run' | raises SendBlocked.
    """
    _check_global_gates()
    if not await _teams_write_allowed(chat_id, sender):
        raise SendBlocked("chat not in teams_write allowlist (policy.yaml)")
    if not _auto_send_allowed(chat_id):
        raise SendBlocked("rate limit: already auto-replied in this conversation recently")
    signed = _signed(body)
    detail = {"conversation_id": chat_id, "body": signed, "kind": kind}
    if db.get_setting("dry_run") == "true":
        db.audit(kind, {**detail, "dry_run": True}, item_id=item_id)
        log.info("DRY RUN %s -> %s: %s", kind, chat_id[:30], signed[:80])
        return "dry_run"
    await graph.send_chat_message(chat_id, signed)
    db.audit(kind, detail, item_id=item_id)
    return "sent"


async def approved_send(draft: dict) -> str:
    """Human-approved draft send (Teams or email). Returns 'sent' | 'dry_run'."""
    _check_global_gates()
    if draft["channel"] == "teams" and not await _teams_write_allowed(
            draft["reply_to_ref"], draft.get("recipient")):
        raise SendBlocked("chat not in teams_write allowlist (policy.yaml); add it to send")
    signed = _signed(draft["body"])
    detail = {"conversation_id": draft.get("reply_to_ref"), "channel": draft["channel"],
              "recipient": draft.get("recipient"), "body": signed}
    if db.get_setting("dry_run") == "true":
        db.audit("approved_send", {**detail, "dry_run": True},
                 item_id=draft.get("item_id"), actor="user")
        return "dry_run"
    if draft["channel"] == "teams":
        await graph.send_chat_message(draft["reply_to_ref"], signed)
    elif draft["channel"] == "email":
        await graph.reply_email(draft["reply_to_ref"], signed)
    else:
        raise SendBlocked(f"unknown channel {draft['channel']}")
    db.audit("approved_send", detail, item_id=draft.get("item_id"), actor="user")
    return "sent"


async def pr_review_chat_post(chat_id: str, body: str, *, item_id: int | None) -> str:
    """Teams post for the PR-review automation. Explicitly requested automation:
    not rate-limited and not gated on away-state, but kill switch, dry run and
    the write allowlist always apply, and the attribution line is appended."""
    _check_global_gates()
    if not await _teams_write_allowed(chat_id):
        raise SendBlocked("chat not in teams_write allowlist (policy.yaml)")
    attribution = policy().get("pr_review", {}).get("attribution", "")
    body = _clean(body)
    if attribution and attribution not in body:
        body = f"{body}\n\n{attribution}"
    detail = {"conversation_id": chat_id, "body": body, "kind": "pr_review_post"}
    if db.get_setting("dry_run") == "true":
        db.audit("pr_review_post", {**detail, "dry_run": True}, item_id=item_id)
        return "dry_run"
    await graph.send_chat_message(chat_id, body)
    db.audit("pr_review_post", detail, item_id=item_id)
    return "sent"


_alex_self_chat_id: str | None = None


async def _resolve_self_chat() -> str | None:
    """Resolve the chat CoS DMs the user in. SAFETY: we never derive this from
    create_chat_with_user(own email) -- on this tenant that returns the user's 1:1
    with a COWORKER, so a nudge would land in their DMs. Only two safe sources:
    an explicitly pinned chat id, or a true Teams self-chat (every member is
    the user). If neither exists, return None and the nudge is skipped."""
    global _alex_self_chat_id
    if _alex_self_chat_id:
        return _alex_self_chat_id
    pol = policy()
    pinned = (pol.get("me", {}).get("self_chat_id")
              or pol.get("pr_readiness", {}).get("nudge_chat_id"))
    if pinned:
        _alex_self_chat_id = pinned
        return _alex_self_chat_id
    me = (pol.get("me", {}).get("aad_id") or "").lower()
    for c in await graph.chats_cached(ttl=60):
        if c.get("chatType") != "oneOnOne":
            continue
        try:
            members = await graph.get_chat_members(c["id"])
        except Exception:
            continue
        ids = [str(m.get("userId") or "").lower() for m in members if m.get("userId")]
        # true self-chat only: at least one member and EVERY member is the user
        if ids and all(i == me for i in ids):
            _alex_self_chat_id = c["id"]
            return _alex_self_chat_id
    return None


async def notify_alex(body: str, *, kind: str, item_id: int | None = None) -> str:
    """Private note to the user in his own Teams self-chat (e.g. PR-readiness nudges).
    A self-note is not outward-facing, so the write allowlist does not apply; the
    kill switch and dry run still do. Returns 'sent' | 'dry_run' | raises SendBlocked."""
    _check_global_gates()
    chat_id = await _resolve_self_chat()
    if not chat_id:
        raise SendBlocked("could not resolve the user's self-chat (set pr_readiness.nudge_chat_id)")
    detail = {"conversation_id": chat_id, "body": body, "kind": kind}
    if db.get_setting("dry_run") == "true":
        db.audit(kind, {**detail, "dry_run": True}, item_id=item_id)
        return "dry_run"
    await graph.send_chat_message(chat_id, body)
    db.audit(kind, detail, item_id=item_id)
    return "sent"


async def weekly_status_post(chat_id: str, body: str, *, item_id: int | None) -> str:
    """Post the approved weekly status summary to its chat. the user explicitly
    approves each one, so it is not rate-limited or away-gated, but the kill
    switch, dry run and the write allowlist all apply. The body is already HTML
    (with @mentions and &mdash; entities) so it is sent as-is; only the
    weekly_status attribution is appended."""
    _check_global_gates()
    if not await _teams_write_allowed(chat_id):
        raise SendBlocked("chat not in teams_write allowlist (policy.yaml)")
    attribution = policy().get("weekly_status", {}).get("attribution", "")
    if attribution and attribution not in body:
        body = f"{body}<p><em>{attribution}</em></p>"
    detail = {"conversation_id": chat_id, "body": body, "kind": "weekly_status_post"}
    if db.get_setting("dry_run") == "true":
        db.audit("weekly_status_post", {**detail, "dry_run": True}, item_id=item_id)
        return "dry_run"
    await graph.send_chat_message(chat_id, body)
    db.audit("weekly_status_post", detail, item_id=item_id)
    return "sent"


async def assistant_chat_post(chat_id: str, body: str, *, kind: str,
                              item_id: int | None,
                              attribution: str | None = None,
                              sender: str | None = None) -> str:
    """Utility post from the assistant (e.g. transcript request in a meeting
    chat). Kill switch, dry run and the write allowlist apply; the neutral
    assistant attribution is appended unless a caller-specific one is given.
    sender enables person-based allowlist rules for 1:1 chats."""
    _check_global_gates()
    if not await _teams_write_allowed(chat_id, sender):
        raise SendBlocked("chat not in teams_write allowlist (policy.yaml)")
    if attribution is None:
        attribution = policy()["messaging"].get("assistant_attribution", "[from the user's assistant]")
    body = _clean(body)
    if attribution not in body:
        body = f"{body}\n\n{attribution}"
    detail = {"conversation_id": chat_id, "body": body, "kind": kind}
    if db.get_setting("dry_run") == "true":
        db.audit(kind, {**detail, "dry_run": True}, item_id=item_id)
        return "dry_run"
    await graph.send_chat_message(chat_id, body)
    db.audit(kind, detail, item_id=item_id)
    return "sent"


async def assistant_channel_post(team_id: str, channel_id: str, body: str, *,
                                 kind: str, item_id: int | None) -> str:
    """Post to a Teams channel. Channels are not chats, so the teams_write
    allowlist can't match them; the only channels the assistant can post to
    are the ones pinned by id in policy.yaml, which is the authorization.
    Kill switch and dry run apply; the assistant attribution is appended."""
    _check_global_gates()
    attribution = policy()["messaging"].get("assistant_attribution", "[from the user's assistant]")
    body = _clean(body)
    if attribution not in body:
        body = f"{body}\n\n{attribution}"
    detail = {"team_id": team_id, "channel_id": channel_id, "body": body, "kind": kind}
    if db.get_setting("dry_run") == "true":
        db.audit(kind, {**detail, "dry_run": True}, item_id=item_id)
        log.info("DRY RUN %s -> channel %s: %s", kind, channel_id[:30], body[:80])
        return "dry_run"
    await graph.post_channel_message(team_id, channel_id, body)
    db.audit(kind, detail, item_id=item_id)
    return "sent"


async def post_github_comment(pr_url: str, body: str, *, item_id: int | None) -> str:
    """GitHub PR comment through gh CLI. Kill switch and dry run apply."""
    _check_global_gates()
    attribution = policy().get("pr_review", {}).get("attribution", "")
    body = _clean(body)
    if attribution and attribution not in body:
        body = f"{body}\n\n{attribution}"
    detail = {"pr_url": pr_url, "body": body}
    if db.get_setting("dry_run") == "true":
        db.audit("github_comment", {**detail, "dry_run": True}, item_id=item_id)
        return "dry_run"
    import asyncio
    proc = await asyncio.create_subprocess_exec(
        "gh", "pr", "comment", pr_url, "--body", body,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise SendBlocked(f"gh pr comment failed: {err.decode()[:200]}")
    db.audit("github_comment", detail, item_id=item_id)
    return "sent"


def notify_mac(title: str, message: str):
    """macOS notification for genuinely urgent tier C items."""
    try:
        safe_t = title.replace('"', "'")[:80]
        safe_m = message.replace('"', "'")[:200]
        subprocess.Popen(
            ["osascript", "-e", f'display notification "{safe_m}" with title "{safe_t}" sound name "Glass"'])
    except Exception:
        log.exception("notification failed")
