"""Data hygiene: keep the working set honest.

A draft written for a two-week-old message is dead weight: approving it would
send a confusing, badly-late reply, and hundreds of them bury the drafts that
matter (the board hit 242 pending drafts before this existed). Stale pending
drafts are marked 'superseded' (never deleted; the audit trail stays intact).

Items stuck in 'new' or 'classified' for too long are auto-dismissed so they
don't clog the pings list forever.

Runs daily and on demand via POST /api/sweep/hygiene.
"""

import logging

from . import db
from .config import policy

log = logging.getLogger("chief.hygiene")

DEFAULT_STALE_DRAFT_DAYS = 3   # replies to 3-day-old threads are always too late
DEFAULT_STALE_ITEM_DAYS = 14


async def sweep() -> dict:
    cfg = policy().get("hygiene", {})
    draft_days = int(cfg.get("stale_draft_days", DEFAULT_STALE_DRAFT_DAYS))
    item_days = int(cfg.get("stale_item_days", DEFAULT_STALE_ITEM_DAYS))

    # Supersede stale pending drafts (bulk: single UPDATE + companion item reset).
    draft_cutoff = db.cutoff(days=draft_days)
    stale_draft_ids = [
        r["id"] for r in db.query(
            "SELECT id FROM drafts WHERE status='pending' AND created_at < ?", (draft_cutoff,))
    ]
    if stale_draft_ids:
        placeholders = ",".join("?" * len(stale_draft_ids))
        db.execute(
            f"UPDATE drafts SET status='superseded', resolved_at=? WHERE id IN ({placeholders})",
            (db.now(), *stale_draft_ids))
        # Tier-C superseded drafts go back to 'new' so the next sweep re-drafts
        # them with overdue context. Tier-A/B just go back to classified (too old to matter).
        db.execute(
            f"UPDATE items SET status='new', action_taken=?, updated_at=? "
            f"WHERE tier='C' AND id IN (SELECT item_id FROM drafts WHERE id IN ({placeholders}))",
            (f"draft superseded (stale > {draft_days}d) — queued for redraft", db.now(), *stale_draft_ids))
        db.execute(
            f"UPDATE items SET status='classified', action_taken=?, updated_at=? "
            f"WHERE tier!='C' AND id IN (SELECT item_id FROM drafts WHERE id IN ({placeholders}))",
            (f"draft superseded (stale > {draft_days}d)", db.now(), *stale_draft_ids))
        db.audit("drafts_superseded",
                 {"count": len(stale_draft_ids), "older_than_days": draft_days,
                  "draft_ids": stale_draft_ids[:50]})
        log.info("hygiene: superseded %d stale pending drafts (>%dd old)",
                 len(stale_draft_ids), draft_days)

    # Auto-dismiss stale new/classified items (Teams + email only; not PR or loops).
    item_cutoff = db.cutoff(days=item_days)
    stale_count = db.query(
        "SELECT COUNT(*) AS n FROM items WHERE status IN ('new','classified') "
        "AND source IN ('teams_chat','teams_channel','email') AND received_at < ?",
        (item_cutoff,))[0]["n"]
    if stale_count:
        db.execute(
            "UPDATE items SET status='dismissed', action_taken=?, updated_at=? "
            "WHERE status IN ('new','classified') "
            "AND source IN ('teams_chat','teams_channel','email') AND received_at < ?",
            (f"auto-dismissed (stale > {item_days}d)", db.now(), item_cutoff))
        db.audit("items_auto_dismissed",
                 {"count": stale_count, "older_than_days": item_days})
        log.info("hygiene: auto-dismissed %d stale items (>%dd old)", stale_count, item_days)

    # Fast-expire tier A pings: covers Teams and email.
    tier_a_hours = int(cfg.get("tier_a_dismiss_hours", 8))
    tier_a_cutoff = db.cutoff(hours=tier_a_hours)
    tier_a_count = db.query(
        "SELECT COUNT(*) AS n FROM items WHERE tier='A' AND status='classified' "
        "AND source IN ('teams_chat','teams_channel','email') AND received_at < ?",
        (tier_a_cutoff,))[0]["n"]
    if tier_a_count:
        db.execute(
            "UPDATE items SET status='dismissed', action_taken=?, updated_at=? "
            "WHERE tier='A' AND status='classified' "
            "AND source IN ('teams_chat','teams_channel','email') AND received_at < ?",
            (f"tier A auto-dismissed (> {tier_a_hours}h, no reply needed)", db.now(), tier_a_cutoff))
        db.audit("tier_a_auto_dismissed", {"count": tier_a_count, "hours": tier_a_hours})
        log.info("hygiene: fast-expired %d tier A items (>%dh old)", tier_a_count, tier_a_hours)

    # Tier-C items where needs_reply=False auto-expire like tier-A.
    # These are VIP/high-priority messages the LLM said require no reply; they
    # should surface briefly but not linger on the board indefinitely.
    tier_c_noreply_hours = int(cfg.get("tier_c_noreply_dismiss_hours", 24))
    tier_c_cutoff = db.cutoff(hours=tier_c_noreply_hours)
    tier_c_noreply_count = db.query(
        "SELECT COUNT(*) AS n FROM items i "
        "WHERE i.tier='C' AND i.status='classified' "
        "AND i.source IN ('teams_chat','teams_channel','email') "
        "AND i.received_at < ? "
        "AND NOT EXISTS (SELECT 1 FROM drafts d WHERE d.item_id=i.id AND d.status='pending') "
        "AND EXISTS (SELECT 1 FROM audit_log al WHERE al.item_id=i.id "
        "  AND al.action='classified' AND json_extract(al.detail,'$.needs_reply')=0)",
        (tier_c_cutoff,))[0]["n"]
    if tier_c_noreply_count:
        db.execute(
            "UPDATE items SET status='dismissed', action_taken=?, updated_at=? "
            "WHERE tier='C' AND status='classified' "
            "AND source IN ('teams_chat','teams_channel','email') AND received_at < ? "
            "AND NOT EXISTS (SELECT 1 FROM drafts d WHERE d.item_id=items.id AND d.status='pending') "
            "AND EXISTS (SELECT 1 FROM audit_log al WHERE al.item_id=items.id "
            "  AND al.action='classified' AND json_extract(al.detail,'$.needs_reply')=0)",
            (f"tier C no-reply auto-dismissed (> {tier_c_noreply_hours}h)", db.now(), tier_c_cutoff))
        db.audit("tier_c_noreply_auto_dismissed",
                 {"count": tier_c_noreply_count, "hours": tier_c_noreply_hours})
        log.info("hygiene: fast-expired %d tier C no-reply items (>%dh old)",
                 tier_c_noreply_count, tier_c_noreply_hours)

    # Auto-dismiss bot PRs (dependabot, renovate) from the approval tracker.
    # The user doesn't review dependency bump PRs; they go through CI automatically.
    bot_pr_count = db.query(
        "SELECT COUNT(*) AS n FROM items WHERE source='pr_approval' AND status='classified' "
        "AND (sender IN ('app/dependabot','renovate[bot]') "
        "  OR LOWER(sender) LIKE '%dependabot%' OR LOWER(sender) LIKE '%renovate%')"
    )[0]["n"]
    if bot_pr_count:
        db.execute(
            "UPDATE items SET status='done', action_taken='bot PR auto-dismissed', updated_at=? "
            "WHERE source='pr_approval' AND status='classified' "
            "AND (sender IN ('app/dependabot','renovate[bot]') "
            "  OR LOWER(sender) LIKE '%dependabot%' OR LOWER(sender) LIKE '%renovate%')",
            (db.now(),))
        db.audit("bot_prs_dismissed", {"count": bot_pr_count})
        log.info("hygiene: dismissed %d bot PRs (dependabot/renovate)", bot_pr_count)

    # Stale tier-B classified items with no pending draft.
    stale_b_cutoff = db.cutoff(days=item_days)
    stale_b_count = db.query(
        "SELECT COUNT(*) AS n FROM items i "
        "WHERE i.tier='B' AND i.status='classified' "
        "AND i.source IN ('teams_chat','teams_channel','email') "
        "AND i.received_at < ? "
        "AND NOT EXISTS (SELECT 1 FROM drafts d WHERE d.item_id=i.id AND d.status='pending')",
        (stale_b_cutoff,))[0]["n"]
    if stale_b_count:
        db.execute(
            "UPDATE items SET status='dismissed', action_taken=?, updated_at=? "
            "WHERE tier='B' AND status='classified' "
            "AND source IN ('teams_chat','teams_channel','email') AND received_at < ? "
            "AND NOT EXISTS (SELECT 1 FROM drafts d WHERE d.item_id=items.id AND d.status='pending')",
            (f"tier B auto-dismissed (stale > {item_days}d, no pending draft)", db.now(), stale_b_cutoff))
        db.audit("tier_b_auto_dismissed",
                 {"count": stale_b_count, "older_than_days": item_days})
        log.info("hygiene: auto-dismissed %d stale tier B items (>%dd, no draft)",
                 stale_b_count, item_days)

    # Auto-dismiss stale open loops by type.
    # email_unanswered: 14d (window closed, reply now is confusing)
    # silent_mention: 7d (old @mentions are no longer actionable)
    # pr_awaiting_me: 21d (PR almost certainly merged/closed by then)
    loop_ages = {
        "email_unanswered": int(cfg.get("loop_email_days", 14)),
        "silent_mention":   int(cfg.get("loop_mention_days", 7)),
        "pr_awaiting_me":   int(cfg.get("loop_pr_days", 21)),
    }
    # Auto-dismiss bot PR review loops (dependabot, renovate) — the user doesn't review these.
    bot_loop_count = db.query(
        "SELECT COUNT(*) AS n FROM items WHERE source='loops' AND subject='pr_awaiting_me' "
        "AND status NOT IN ('dismissed','done') "
        "AND (LOWER(content) LIKE '%dependabot%' OR LOWER(content) LIKE '%renovate%' "
        "  OR LOWER(content) LIKE '%build(deps)%' OR LOWER(content) LIKE '%chore(deps)%')"
    )[0]["n"]
    if bot_loop_count:
        db.execute(
            "UPDATE items SET status='dismissed', action_taken='bot PR loop auto-dismissed', updated_at=? "
            "WHERE source='loops' AND subject='pr_awaiting_me' "
            "AND status NOT IN ('dismissed','done') "
            "AND (LOWER(content) LIKE '%dependabot%' OR LOWER(content) LIKE '%renovate%' "
            "  OR LOWER(content) LIKE '%build(deps)%' OR LOWER(content) LIKE '%chore(deps)%')",
            (db.now(),))
        db.audit("bot_loops_dismissed", {"count": bot_loop_count})
        log.info("hygiene: dismissed %d bot PR review loops (dependabot/renovate)", bot_loop_count)
    loop_dismissed = bot_loop_count

    for loop_type, days in loop_ages.items():
        cutoff = db.cutoff(days=days)
        # Loop type stored in subject (type_ column is always 'open_loop')
        n = db.query(
            "SELECT COUNT(*) AS n FROM items WHERE source='loops' AND subject=? "
            "AND status NOT IN ('dismissed','done') AND received_at < ?",
            (loop_type, cutoff))[0]["n"]
        if n:
            db.execute(
                "UPDATE items SET status='dismissed', action_taken=?, updated_at=? "
                "WHERE source='loops' AND subject=? AND status NOT IN ('dismissed','done') "
                "AND received_at < ?",
                (f"open loop auto-aged (>{days}d, type={loop_type})", db.now(), loop_type, cutoff))
            db.audit("loop_auto_dismissed", {"count": n, "type": loop_type, "older_than_days": days})
            log.info("hygiene: auto-dismissed %d stale %s loops (>%dd old)", n, loop_type, days)
            loop_dismissed += n

    # Prune audit_log older than retention window (default 90 days).
    # Keeps the table bounded as sweeps accumulate thousands of rows/day.
    audit_days = int(cfg.get("audit_retention_days", 90))
    audit_cutoff = db.cutoff(days=audit_days)
    audit_pruned = 0
    audit_count = db.query(
        "SELECT COUNT(*) AS n FROM audit_log WHERE ts < ?", (audit_cutoff,))[0]["n"]
    if audit_count:
        db.execute("DELETE FROM audit_log WHERE ts < ?", (audit_cutoff,))
        audit_pruned = audit_count
        log.info("hygiene: pruned %d audit_log rows older than %dd", audit_pruned, audit_days)

    return {
        "superseded": len(stale_draft_ids), "draft_older_than_days": draft_days,
        "items_dismissed": stale_count, "item_older_than_days": item_days,
        "tier_a_dismissed": tier_a_count, "tier_a_hours": tier_a_hours,
        "tier_c_noreply_dismissed": tier_c_noreply_count, "tier_c_noreply_hours": tier_c_noreply_hours,
        "bot_prs_dismissed": bot_pr_count,
        "tier_b_dismissed": stale_b_count,
        "bot_loops_dismissed": bot_loop_count,
        "loops_dismissed": loop_dismissed,
        "audit_pruned": audit_pruned, "audit_retention_days": audit_days,
    }
