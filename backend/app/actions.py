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
    """Board-managed (settings table) per-chat/channel adds and removes.
    A remove beats every rule, including policy.yaml and categories. Channels
    use the key f"{team_id}|{channel_id}" (same set as chats - no collision,
    since chat ids and that key format never overlap)."""
    import json as _json
    adds = set(_json.loads(db.get_setting("teams_write_add", "[]")))
    removes = set(_json.loads(db.get_setting("teams_write_remove", "[]")))
    return adds, removes


def channel_key(team_id: str, channel_id: str) -> str:
    return f"{team_id}|{channel_id}"


def board_read_overrides() -> tuple[set[str], set[str]]:
    """Board-managed per-chat/channel read overrides (owner decision, 2026-07-16).
    Reading has always defaulted to on everywhere; this is an explicit
    opt-out only - a remove blocks reading regardless of anything else."""
    import json as _json
    adds = set(_json.loads(db.get_setting("teams_read_add", "[]")))
    removes = set(_json.loads(db.get_setting("teams_read_remove", "[]")))
    return adds, removes


def read_allowed(entity_id: str) -> bool:
    """True unless this chat/channel has been explicitly disabled for
    reading on the Write Access board (default stays permissive, matching
    the long-standing "reading is unrestricted" design)."""
    _, removes = board_read_overrides()
    return entity_id not in removes


def channel_write_allowed(team_id: str, channel_id: str) -> bool:
    """Channels aren't chats, so the chat allowlist doesn't apply - being
    pinned by id in policy.yaml IS the base authorization (assistant_channel_
    post's own docstring), and a board removal on top of that still blocks."""
    _, removes = board_write_overrides()
    return channel_key(team_id, channel_id) not in removes


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
              "recipient": draft.get("recipient"), "body": signed,
              "edited": bool(draft.get("_edited"))}
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
    one_on_ones = [c for c in await graph.chats_cached(ttl=60)
                   if c.get("chatType") == "oneOnOne"]

    # Parallel-fetch all member lists (was N sequential awaits, one per 1:1 chat).
    async def _get_members(c):
        try:
            return c, await graph.get_chat_members(c["id"])
        except Exception:
            return c, []

    results = await asyncio.gather(*[_get_members(c) for c in one_on_ones])
    for c, members in results:
        ids = [str(m.get("userId") or "").lower() for m in members if m.get("userId")]
        # true self-chat only: at least one member and EVERY member is the user
        if ids and all(i == me for i in ids):
            _alex_self_chat_id = c["id"]
            return _alex_self_chat_id
    return None


async def notify_alex(body: str, *, kind: str, item_id: int | None = None) -> str:
    """Private note to the user in their own Teams self-chat (e.g. PR-readiness nudges).
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


async def companion_reply(body: str) -> str:
    """CoS's reply in the user's companion chat (their own Teams self-chat — not
    outward-facing, only the user reads it). Kill switch applies; dry run applies
    unless companion.live (the owner's exception, 2026-07-10: the companion is their
    away-mode remote control, it must answer while dry run is on)."""
    _check_global_gates()
    chat_id = (policy().get("companion", {}) or {}).get("chat_id") or await _resolve_self_chat()
    if not chat_id:
        raise SendBlocked("no companion chat (set companion.chat_id or me.self_chat_id)")
    if not body.startswith("🤖"):
        body = f"🤖 {body}"
    detail = {"conversation_id": chat_id, "body": body, "kind": "companion_reply"}
    if db.get_setting("dry_run") == "true" and not bool(
            policy().get("companion", {}).get("live", True)):
        db.audit("companion_reply", {**detail, "dry_run": True})
        return "dry_run"
    await graph.send_chat_message(chat_id, body)
    db.audit("companion_reply", detail)
    return "sent"


async def weekly_status_post(chat_id: str, body: str, *, item_id: int | None) -> str:
    """Post the approved weekly status summary to its chat. The user explicitly
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
                                 kind: str, item_id: int | None,
                                 reply_to: str | None = None,
                                 attribute: bool = True) -> str:
    """Post to a Teams channel. Channels are not chats, so the teams_write
    allowlist can't match them; the only channels the assistant can post to
    are the ones pinned by id in policy.yaml, which is the authorization.
    Kill switch and dry run apply; the assistant attribution is appended
    unless attribute=False (e.g. an in-thread reply that already reads as
    CoS speaking, where re-stating it every turn would feel robotic).
    reply_to posts as a threaded reply to that message id instead of a new
    top-level post."""
    _check_global_gates()
    if not channel_write_allowed(team_id, channel_id):
        raise SendBlocked("channel writing disabled on the Write Access board")
    body = _clean(body)
    if attribute:
        attribution = policy()["messaging"].get("assistant_attribution", "[from the user's assistant]")
        if attribution not in body:
            body = f"{body}\n\n{attribution}"
    detail = {"team_id": team_id, "channel_id": channel_id, "body": body, "kind": kind,
             "reply_to": reply_to}
    if db.get_setting("dry_run") == "true":
        db.audit(kind, {**detail, "dry_run": True}, item_id=item_id)
        log.info("DRY RUN %s -> channel %s: %s", kind, channel_id[:30], body[:80])
        return "dry_run"
    await graph.post_channel_message(team_id, channel_id, body, reply_to=reply_to)
    db.audit(kind, detail, item_id=item_id)
    return "sent"


async def assistant_channel_react(team_id: str, channel_id: str, message_id: str, reaction: str, *,
                                  kind: str, item_id: int | None) -> str:
    """React to a Teams channel message. Same authorization model as
    assistant_channel_post (pinned by id in policy.yaml). Kill switch and
    dry run apply; a reaction is visible to the channel like any other post."""
    _check_global_gates()
    if not channel_write_allowed(team_id, channel_id):
        raise SendBlocked("channel writing disabled on the Write Access board")
    detail = {"team_id": team_id, "channel_id": channel_id, "message_id": message_id,
             "reaction": reaction, "kind": kind}
    if db.get_setting("dry_run") == "true":
        db.audit(kind, {**detail, "dry_run": True}, item_id=item_id)
        return "dry_run"
    await graph.add_channel_reaction(team_id, channel_id, message_id, reaction)
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


async def submit_github_review(pr_url: str, body: str, event: str, *,
                               item_id: int | None) -> str:
    """Actually submit a formal GitHub PR review (Approve / Request changes) -
    not just a comment. The owner decided (2026-07-21): CoS was only ever SAYING
    "approved" in Teams/PR-comment prose without ever touching the PR's real
    review state, so it never counted as a genuine approval anyone (or branch
    protection) could see. Same gates as post_github_comment: kill switch and
    dry run apply, since this is now a real, GitHub-visible action taken as
    the user."""
    _check_global_gates()
    if event not in ("approve", "request_changes", "comment"):
        raise ValueError(f"submit_github_review: invalid event {event!r}")
    body = _clean(body)
    detail = {"pr_url": pr_url, "event": event, "body": body}
    if db.get_setting("dry_run") == "true":
        db.audit("github_review", {**detail, "dry_run": True}, item_id=item_id)
        return "dry_run"
    import asyncio
    flag = {"approve": "--approve", "request_changes": "--request-changes",
            "comment": "--comment"}[event]
    proc = await asyncio.create_subprocess_exec(
        "gh", "pr", "review", pr_url, flag, "--body", body,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise SendBlocked(f"gh pr review failed: {err.decode()[:200]}")
    db.audit("github_review", detail, item_id=item_id)
    return "sent"


def _ado_live() -> bool:
    """swarm.receipts.ado_live: the owner's explicit exception (2026-07-09) — swarm
    comments on ADO work items post for real even while global dry run is on.
    The kill switch still blocks everything."""
    return bool(policy().get("swarm", {}).get("receipts", {}).get("ado_live", False))


async def ado_comment_post(item_id: int, markdown: str, *, project: str,
                           digest: str | None = None) -> str:
    """Swarm receipt: evidence comment on an ADO work item. Kill switch always
    applies; dry run applies unless receipts.ado_live. digest fingerprints the
    content for the anti-spam gate. Returns 'sent' | 'dry_run'."""
    _check_global_gates()
    body = _clean(markdown)
    detail = {"ado_item": item_id, "project": project, "body": body,
              "kind": "swarm_ado_comment", "digest": digest}
    if db.get_setting("dry_run") == "true" and not _ado_live():
        db.audit("swarm_ado_comment", {**detail, "dry_run": True}, actor="swarm")
        log.info("DRY RUN swarm comment -> ADO #%s: %s", item_id, body[:80])
        return "dry_run"
    from . import ado
    await asyncio.to_thread(ado.post_comment, project, item_id, body)
    db.audit("swarm_ado_comment", detail, actor="swarm")
    return "sent"


async def ado_state_update(item_id: int, state: str, *, project: str,
                           reason: str = "", resolution_note: str = "") -> str:
    """Swarm sync: move an ADO work item's state when its PR merges (the owner:
    'always in sync with github ... update ticket status'). Kill switch always
    applies; dry run applies unless receipts.ado_live — the same exception as
    swarm comments. Returns 'sent' | 'dry_run'."""
    _check_global_gates()
    from . import ado
    detail = {"ado_item": item_id, "project": project, "state": state,
              "reason": reason, "kind": "swarm_ado_state"}
    # idempotent: the pipeline flips states at several stages — never PATCH
    # a ticket that is already where it should be (audited so reconcile
    # sweeps see the state is settled and stop re-checking)
    items = await asyncio.to_thread(ado.items_by_ids, [item_id])
    if items and items[0]["state"] == state:
        db.audit("swarm_ado_state", {**detail, "already": True}, actor="swarm")
        return "already"
    if db.get_setting("dry_run") == "true" and not _ado_live():
        db.audit("swarm_ado_state", {**detail, "dry_run": True}, actor="swarm")
        log.info("DRY RUN swarm state -> ADO #%s: %s", item_id, state)
        return "dry_run"
    await asyncio.to_thread(ado.set_state, project, item_id, state, resolution_note)
    db.audit("swarm_ado_state", detail, actor="swarm")
    return "sent"


async def ado_iteration_update(item_id: int, iteration: str, *, project: str,
                               reason: str = "") -> str:
    """Swarm hygiene fix: move an ADO work item to another iteration (e.g.
    deferred-per-triage work parked back to the backlog root). Kill switch
    always applies; dry run applies unless receipts.ado_live — the same
    exception as swarm comments. Returns 'sent' | 'already' | 'dry_run'."""
    _check_global_gates()
    from . import ado
    detail = {"ado_item": item_id, "project": project, "iteration": iteration,
              "reason": reason, "kind": "swarm_ado_iteration"}
    # idempotent: never PATCH an item already in the target iteration
    items = await asyncio.to_thread(ado.items_by_ids, [item_id])
    if items and items[0]["iteration"] == iteration.split("\\")[-1]:
        db.audit("swarm_ado_iteration", {**detail, "already": True}, actor="swarm")
        return "already"
    if db.get_setting("dry_run") == "true" and not _ado_live():
        db.audit("swarm_ado_iteration", {**detail, "dry_run": True}, actor="swarm")
        log.info("DRY RUN swarm iteration -> ADO #%s: %s", item_id, iteration)
        return "dry_run"
    await asyncio.to_thread(ado.set_iteration, project, item_id, iteration)
    db.audit("swarm_ado_iteration", detail, actor="swarm")
    return "sent"


async def ado_clarify_post(item_id: int, markdown: str, *, project: str) -> str:
    """Swarm clarification request: a comment asking a human to sharpen the
    ticket (unclear acceptance criteria etc.). Same gates as every outbound
    action; audited as swarm_clarify so the swarm asks each item only once."""
    _check_global_gates()
    body = _clean(markdown)
    detail = {"ado_item": item_id, "project": project, "body": body,
              "kind": "swarm_clarify"}
    if db.get_setting("dry_run") == "true" and not _ado_live():
        db.audit("swarm_clarify", {**detail, "dry_run": True}, actor="swarm")
        log.info("DRY RUN swarm clarify -> ADO #%s", item_id)
        return "dry_run"
    from . import ado
    await asyncio.to_thread(ado.post_comment, project, item_id, body)
    db.audit("swarm_clarify", detail, actor="swarm")
    return "sent"


async def swarm_summary_post(markdown: str) -> str:
    """Swarm receipt: run-summary table to the Teams chat pinned in policy
    swarm.receipts (summary_chat_id wins; summary_chat_topic is resolved against
    readable chats). Kill switch, dry run and the write allowlist all apply."""
    _check_global_gates()
    rc = policy().get("swarm", {}).get("receipts", {}) or {}
    chat_id = rc.get("summary_chat_id")
    if not chat_id and rc.get("summary_chat_topic"):
        topic = rc["summary_chat_topic"].strip().lower()
        chat_id = next((c["id"] for c in await graph.chats_cached()
                        if (c.get("topic") or "").strip().lower() == topic), None)
    if not chat_id:
        raise SendBlocked("no swarm.receipts.summary_chat_id/summary_chat_topic configured")
    if not await _teams_write_allowed(chat_id):
        raise SendBlocked("chat not in teams_write allowlist (policy.yaml)")
    body = _clean(markdown)
    detail = {"conversation_id": chat_id, "body": body, "kind": "swarm_summary_post"}
    if db.get_setting("dry_run") == "true":
        db.audit("swarm_summary_post", {**detail, "dry_run": True}, actor="swarm")
        log.info("DRY RUN swarm summary -> %s: %s", chat_id[:30], body[:80])
        return "dry_run"
    await graph.send_chat_message(chat_id, body)
    db.audit("swarm_summary_post", detail, actor="swarm")
    return "sent"


def _pr_live() -> bool:
    """swarm.code.pr_live: the owner's explicit exception (2026-07-09) — committed
    swarm work ships as DRAFT PRs even while global dry run is on. The kill
    switch still blocks everything."""
    return bool(policy().get("swarm", {}).get("code", {}).get("pr_live", False))


async def swarm_pr_push_fix(wt: dict) -> str:
    """Push a CI-fix commit to an existing swarm PR branch (--no-verify;
    GitHub CI is the verifier). Kill switch always; dry run unless pr_live."""
    _check_global_gates()
    detail = {"branch": wt["branch"], "repo": wt["repo"], "pr_url": wt.get("pr_url"),
              "kind": "swarm_ci_push"}
    if db.get_setting("dry_run") == "true" and not _pr_live():
        db.audit("swarm_ci_push", {**detail, "dry_run": True}, actor="swarm")
        return "dry_run"
    proc = await asyncio.create_subprocess_exec(
        "git", "push", "--no-verify", "origin", wt["branch"], cwd=wt["path"],
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise SendBlocked(f"ci fix push failed: {err.decode()[:200]}")
    db.audit("swarm_ci_push", detail, actor="swarm")
    return "sent"


def _uat_chat_live() -> bool:
    """swarm.receipts.uat_chat_live: the owner's explicit exception (2026-07-10) —
    UAT-chat escalations and verified-updates post for real even while global
    dry run is on. The kill switch still blocks everything."""
    return bool(policy().get("swarm", {}).get("receipts", {}).get("uat_chat_live", False))


async def swarm_uat_chat_post(body: str, *, kind: str, item_id: int | None = None) -> str:
    """Post to the UAT team meeting chat (receipts.uat_chat_id): clarify
    escalations when ticket questions go unanswered, and short updates when a
    change is verified in UAT. Kill switch + teams_write allowlist always
    apply; dry run applies unless receipts.uat_chat_live. Audited as
    swarm_uat_chat (kind + ado_item drive the dedupe)."""
    _check_global_gates()
    chat_id = (policy().get("swarm", {}).get("receipts", {}) or {}).get("uat_chat_id")
    if not chat_id:
        return "skipped: no receipts.uat_chat_id configured"
    if not await _teams_write_allowed(chat_id):
        raise SendBlocked("chat not in teams_write allowlist (policy.yaml)")
    msg = _clean(body)
    detail = {"conversation_id": chat_id, "body": msg, "kind": kind, "ado_item": item_id}
    if db.get_setting("dry_run") == "true" and not _uat_chat_live():
        db.audit("swarm_uat_chat", {**detail, "dry_run": True}, actor="swarm")
        return "dry_run"
    await graph.send_chat_message(chat_id, msg)
    db.audit("swarm_uat_chat", detail, actor="swarm")
    log.info("swarm UAT chat post (%s) for item %s", kind, item_id)
    return "sent"


async def swarm_pr_announce(wt: dict, url: str) -> str:
    """Announce a shipped swarm PR in the PR review chat (receipts.pr_chat_id).
    Loud about every skip reason; audited as swarm_pr_chat when sent."""
    chat_id = (policy().get("swarm", {}).get("receipts", {}) or {}).get("pr_chat_id")
    if not chat_id:
        log.warning("swarm PR announce: no receipts.pr_chat_id configured")
        return "skipped: no pr_chat_id"
    if not url:
        log.warning("swarm PR announce: no PR url for %s", wt.get("branch"))
        return "skipped: no url"
    if not await _teams_write_allowed(chat_id):
        log.warning("swarm PR announce: chat %s not write-allowed", chat_id[:30])
        return "blocked: not in teams_write allowlist"
    ab = wt.get("target_ref") or ""
    title = (f"{ab}: {wt.get('title', '')}" if ab.startswith("AB#") else wt.get("title", ""))[:120]
    msg = _clean(f"🤖 {title} is ready for a look - checks are green: {url}\n"
                 f"Swarm-built on {wt.get('branch')}; one approval and it can merge.")
    detail = {"conversation_id": chat_id, "body": msg, "kind": "swarm_pr_chat",
              "pr_url": url}
    if db.get_setting("dry_run") == "true" and not _pr_live():
        db.audit("swarm_pr_chat", {**detail, "dry_run": True}, actor="swarm")
        return "dry_run"
    await graph.send_chat_message(chat_id, msg)
    db.audit("swarm_pr_chat", detail, actor="swarm")
    log.info("swarm PR announced in chat: %s", url)
    return "sent"


async def swarm_pr_open(wt: dict) -> dict:
    """Committed swarm worktree -> push the branch and open a DRAFT PR.
    Kill switch always applies; dry run applies unless code.pr_live.
    Returns {status: 'sent'|'dry_run', url?}."""
    _check_global_gates()
    # AB#xxxx in the title makes Azure Boards auto-link the PR to the work item
    ab = wt.get("target_ref") or ""
    title = (f"{ab}: {wt['title']}" if ab.startswith("AB#") else f"{wt['title']}")[:120]
    body = _clean(
        f"{wt.get('summary') or 'Automated change by the CoS swarm.'}\n\n"
        f"Addresses: {wt.get('target_ref') or 'n/a'}\n\n"
        "Opened as a draft by the CoS swarm after human approval on the board.")
    detail = {"worktree_id": wt["id"], "repo": wt["repo"], "branch": wt["branch"],
              "base": wt["base_ref"], "title": title, "kind": "swarm_pr_open"}
    if db.get_setting("dry_run") == "true" and not _pr_live():
        db.audit("swarm_pr_open", {**detail, "dry_run": True}, actor="user")
        log.info("DRY RUN swarm PR -> %s %s", wt["repo"], wt["branch"])
        return {"status": "dry_run"}

    async def _run(*args: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=wt["path"],
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise SendBlocked(f"{args[0]} {args[1]} failed: {err.decode()[:300]}")
        return out.decode()

    await _run("git", "push", "--no-verify", "-u", "origin", wt["branch"])
    out = await _run("gh", "pr", "create", "--draft", "--title", title,
                     "--body", body, "--base", wt["base_ref"] or "dev")
    url = next((l.strip() for l in out.splitlines() if l.strip().startswith("http")), "")
    db.execute("UPDATE swarm_worktrees SET status='pr_created', pr_url=?, updated_at=? "
               "WHERE id=?", (url, db.now(), wt["id"]))
    db.audit("swarm_pr_open", {**detail, "url": url}, actor="user")
    # write the PR link + branch back onto the ADO ticket
    if ab.startswith("AB#"):
        try:
            rows = db.query("SELECT item_project FROM swarm_jobs WHERE id=?",
                            (wt.get("job_id"),)) if wt.get("job_id") else []
            from .worktrees import _default_project
            project = (rows[0]["item_project"] if rows else "") or _default_project()
            await ado_comment_post(
                int(ab[3:]),
                (f"🤖 <b>Run #{wt.get('run_id', '?')}</b>: opened draft PR "
                 f"<a href=\"{url}\">{url}</a> from branch <code>{wt['branch']}</code> "
                 f"in {wt['repo']}."),
                project=project)
        except Exception:
            log.exception("PR-link comment on the ticket failed (PR itself is open)")
    try:
        await swarm_pr_announce(wt, url)
    except Exception:
        log.exception("swarm PR announce failed (PR itself is open)")
    # workers are cattle: the branch is pushed and the PR is the receipt, so
    # the local worktree has nothing left to hold — clean it up best-effort
    try:
        from . import worktrees as _wt
        repo_dir = _wt._repo_dir(wt["repo"])
        if repo_dir:
            await _wt._git(repo_dir, "worktree", "remove", "--force", wt["path"], check=False)
            await _wt._git(repo_dir, "worktree", "prune", check=False)
    except Exception:
        log.exception("worktree cleanup after PR failed (harmless)")
    return {"status": "sent", "url": url}


def notify_mac(title: str, message: str):
    """macOS notification for genuinely urgent tier C items."""
    try:
        safe_t = title.replace('"', "'")[:80]
        safe_m = message.replace('"', "'")[:200]
        subprocess.Popen(
            ["osascript", "-e", f'display notification "{safe_m}" with title "{safe_t}" sound name "Glass"'])
    except Exception:
        log.exception("notification failed")
