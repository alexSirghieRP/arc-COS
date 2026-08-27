"""Deployed agent-pipeline monitoring sweep.

Polls the live pipeline (via app.osca → Firestore), persists a health snapshot
for trend history, and reconciles the problem set into `osca_problems` so the
notification center has a durable, ack-able feed:

  - problems seen this round      → upserted (first_seen kept, last_seen bumped;
                                     resolved ones re-open)
  - problems no longer present    → marked resolved (their fix propagated)

New high-severity problems also drop a line into Chieff's audit log so they show
up in the activity feed. Runs on a short interval (policy: osca.sweep_minutes).
"""

import json
import logging

from .. import db, osca

log = logging.getLogger("chief.sweep.osca")


async def sweep() -> dict:
    data = await osca.get_full(force=True)
    if not data.get("ok"):
        db.audit("osca_poll_error", {"error": data.get("error", "unknown")})
        return {"ok": False, "error": data.get("error")}

    totals = data.get("totals", {})
    now = db.now()

    # 1) snapshot for trend history
    db.execute(
        "INSERT INTO osca_snapshots"
        "(ts,environment,status,active_clients,escalations,paused,blocked,orphaned,dead_letter,detail) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (now, data.get("environment"), data.get("status"),
         totals.get("active_clients", 0), totals.get("escalations", 0),
         totals.get("paused", 0), totals.get("blocked", 0),
         totals.get("orphaned", 0), totals.get("dead_letter", 0),
         json.dumps({"stages": data.get("stages"), "milestones": data.get("milestones"),
                     "flow": data.get("flow"), "throughput": data.get("throughput")})),
    )

    # 2) reconcile problem groups
    groups = data.get("groups", [])
    seen_keys = {g["key"] for g in groups}
    existing = {r["key"]: r for r in db.query(
        "SELECT key, status, recent_count FROM osca_problems")}

    new_high = 0
    stale: list[str] = []
    with db.transaction():
        for g in groups:
            examples = json.dumps(g.get("examples", []))
            prev = existing.get(g["key"])
            if prev is None:
                db.execute(
                    "INSERT INTO osca_problems"
                    "(key,kind,severity,title,detail,entity,stage,reason,"
                    "count,recent_count,examples,first_seen,last_seen,source_ts,status) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active')",
                    (g["key"], g["kind"], g["severity"], g["title"], g.get("reason", ""),
                     "", g.get("stage", ""), g.get("reason", ""),
                     g.get("count", 1), g.get("recent_count", 0), examples,
                     now, now, g.get("latest_ts")))
                if g["severity"] == "high":
                    new_high += 1
            else:
                # bump last_seen + counts; re-open if it came back after being
                # resolved, or if it grew since the user acknowledged it (an ack
                # covers the problem as it was, not silent growth).
                new_status = prev["status"]
                if prev["status"] == "resolved":
                    new_status = "active"
                elif (prev["status"] == "acknowledged"
                      and g.get("recent_count", 0) > (prev["recent_count"] or 0)):
                    new_status = "active"
                    db.audit("osca_problem_regrew",
                             {"key": g["key"], "was": prev["recent_count"],
                              "now": g.get("recent_count", 0)})
                db.execute(
                    "UPDATE osca_problems SET last_seen=?, severity=?, title=?, detail=?, "
                    "reason=?, count=?, recent_count=?, examples=?, source_ts=?, status=? "
                    "WHERE key=?",
                    (now, g["severity"], g["title"], g.get("reason", ""),
                     g.get("reason", ""), g.get("count", 1), g.get("recent_count", 0),
                     examples, g.get("latest_ts"), new_status, g["key"]))

        # anything active/ack'd but no longer present → resolved
        stale = [k for k, r in existing.items()
                 if k not in seen_keys and r["status"] != "resolved"]
        if stale:  # noqa: SIM102
            ph = ",".join("?" * len(stale))
            db.execute(
                f"UPDATE osca_problems SET status='resolved', resolved_at=? "
                f"WHERE key IN ({ph})", (now, *stale))

    summary = {
        "ok": True, "status": data.get("status"),
        "active_clients": totals.get("active_clients", 0),
        "groups": len(groups), "new_high": new_high,
        "resolved": len(stale),
    }
    if new_high:
        db.audit("osca_new_problems", {"count": new_high, "status": data.get("status")})
    log.info("osca sweep: status=%s clients=%d groups=%d new_high=%d",
             data.get("status"), totals.get("active_clients", 0), len(groups), new_high)
    return summary
