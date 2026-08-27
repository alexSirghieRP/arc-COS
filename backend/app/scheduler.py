"""APScheduler wiring. Intervals come from policy.yaml and re-apply on change."""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import db
from .config import policy

log = logging.getLogger("chief.scheduler")

scheduler = AsyncIOScheduler()
_applied: dict = {}


def _cron(hhmm: str) -> CronTrigger:
    h, m = hhmm.split(":")
    return CronTrigger(hour=int(h), minute=int(m))


def _cron_dow(hhmm: str, dow: str) -> CronTrigger:
    h, m = hhmm.split(":")
    return CronTrigger(day_of_week=dow, hour=int(h), minute=int(m))


def apply_schedules():
    """(Re)apply schedules from policy.yaml; called at startup and re-checked each sweep."""
    global _applied
    sched = policy()["schedules"]
    if sched == _applied:
        return
    from .sweeps import email as email_sweep
    from .sweeps import open_loops as loops_sweep
    from .sweeps import teams as teams_sweep
    from .sweeps import pr_review as pr_review_sweep
    from .sweeps import transcripts as transcripts_sweep
    from .daily import ensure_daily_note, evening_shutdown, morning_brief

    jobs = {
        "teams_sweep": (teams_sweep.sweep, IntervalTrigger(minutes=sched["teams_sweep_minutes"])),
        "email_sweep": (email_sweep.sweep, IntervalTrigger(minutes=sched["email_sweep_minutes"])),
        "open_loops": (loops_sweep.sweep, IntervalTrigger(minutes=sched["open_loops_minutes"])),
        "transcripts": (transcripts_sweep.sweep,
                        IntervalTrigger(minutes=sched.get("transcripts_sweep_minutes", 15))),
        "pr_review": (pr_review_sweep.sweep,
                      IntervalTrigger(minutes=policy().get("pr_review", {}).get("sweep_minutes", 5))),
        "pr_channel_review": (_pr_channel_review_job,
                              IntervalTrigger(minutes=policy().get("pr_channel_review", {})
                                              .get("sweep_minutes", 5))),
        "pr_digest": (_pr_digest_job,
                      _cron(policy().get("pr_review", {}).get("digest_time", "09:30"))),
        "chieff_daily_update": (_chieff_update_job,
                                _cron(policy().get("chieff", {}).get("daily_update", {})
                                      .get("time", "17:30"))),
        "knowledge_sync": (_knowledge_sync_job,
                           IntervalTrigger(minutes=policy().get("knowledge", {})
                                           .get("sync_minutes", 30))),
        "pr_status": (_pr_status_job,
                      IntervalTrigger(minutes=policy().get("pr_review", {})
                                      .get("status_sweep_minutes", 15))),
        "pr_readiness": (_pr_readiness_job,
                         IntervalTrigger(minutes=policy().get("pr_readiness", {})
                                         .get("sweep_minutes", 20))),
        "ensure_daily_note": (ensure_daily_note,
                              _cron(sched.get("ensure_daily_note", "09:30"))),
        "hygiene": (_hygiene_job, IntervalTrigger(minutes=int(
            policy().get("hygiene", {}).get("run_every_minutes", 120)))),
        "morning_brief": (morning_brief, _cron(sched["morning_brief"])),
        "evening_shutdown": (evening_shutdown, _cron(sched["evening_shutdown"])),
        "weekly_status": (_weekly_status_job, _cron_dow(
            policy().get("weekly_status", {}).get("time", "17:00"),
            policy().get("weekly_status", {}).get("day", "fri"))),
        "scrum": (_scrum_job, _cron_dow(
            policy().get("scrum", {}).get("time", "08:30"), "mon-fri")),
        "pre_meeting": (_pre_meeting_job,
                        IntervalTrigger(minutes=policy().get("pre_meeting", {})
                                        .get("check_minutes", 5))),
        "post_meeting": (_post_meeting_job,
                         IntervalTrigger(minutes=policy().get("post_meeting", {})
                                         .get("check_minutes", 3))),
        "warm_caches": (_warm_caches_job, IntervalTrigger(minutes=5)),
        # registered but off by default: swarm.sweep no-ops unless
        # policy swarm.schedule_enabled is true (manual runs are unaffected)
        "swarm": (_swarm_job,
                  _cron(policy().get("swarm", {}).get("schedule_time", "10:30"))),
        # the pipeline heartbeat: ship/CI-watch/fix/announce between runs
        "swarm_housekeeping": (_swarm_housekeeping_job, IntervalTrigger(
            minutes=int(policy().get("swarm", {}).get("housekeeping_minutes", 10)))),
        "osca_monitor": (_osca_monitor_job,
                         IntervalTrigger(minutes=policy().get("osca", {})
                                         .get("sweep_minutes", 5))),
        "graph_reauth": (_graph_reauth_job,
                         IntervalTrigger(minutes=policy().get("graph_reauth", {})
                                         .get("check_minutes", 10))),
        # configurable channel/process watchers; each honors its own interval
        "watchers": (_watchers_job, IntervalTrigger(minutes=2)),
        # NOTE: the companion's REPLY loop runs in main lifespan (adaptive,
        # sub-second). This is its PROACTIVE side: initiates a heads-up when
        # something material changes, plus the morning brief.
        "companion_proactive": (_companion_proactive_job, IntervalTrigger(
            minutes=int(policy().get("companion", {}).get("proactive_minutes", 5)))),
    }
    # Once-a-day cron jobs must survive a missed slot (Mac asleep / event loop
    # stalled at the exact scheduled second). APScheduler's default
    # misfire_grace_time is 1s, so a miss of even a few minutes silently DROPS
    # the run -- and a daily job that fires once has no second chance that day.
    # None = unlimited grace: run as soon as the loop is free again. Combined
    # with coalesce, at most one catch-up run happens.
    for job_id, (fn, trigger) in jobs.items():
        scheduler.add_job(_wrap(fn, job_id), trigger, id=job_id, replace_existing=True,
                          coalesce=True, max_instances=1, misfire_grace_time=None)
    _applied = sched
    log.info("schedules applied: %s", sched)


async def _pr_digest_job():
    from .sweeps.github_digest import digest
    return await digest()


async def _hygiene_job():
    from .hygiene import sweep
    return await sweep()


async def _chieff_update_job():
    from .sweeps.chieff import daily_update
    return await daily_update()


async def _knowledge_sync_job():
    from .sweeps.knowledge import sync
    return await sync()


async def _pr_status_job():
    from .sweeps.pr_status import sweep
    return await sweep()


async def _pr_channel_review_job():
    from .sweeps.pr_channel_review import sweep
    return await sweep()


async def _pr_readiness_job():
    from .sweeps.pr_readiness import sweep
    return await sweep()


async def _weekly_status_job():
    """Friday: generate the week's draft report (the user approves on the board)."""
    from .sweeps.weekly_status import generate
    return await generate()


async def _scrum_job():
    """Weekday mornings: generate today's scrum ticket (yesterday's work, or
    Friday+weekend on a Monday)."""
    from .sweeps.scrum import generate
    return await generate()


async def _pre_meeting_job():
    from .sweeps.pre_meeting import sweep
    return await sweep()


async def _post_meeting_job():
    from .sweeps.post_meeting import sweep
    return await sweep()


async def _osca_monitor_job():
    from .sweeps.osca import sweep
    return await sweep()


async def _graph_reauth_job():
    from .graph_reauth import maybe_reauth
    return await maybe_reauth()


async def _companion_proactive_job():
    from .companion import proactive_sweep
    return await proactive_sweep()


async def _watchers_job():
    from .watchers import sweep
    return await sweep()


async def _swarm_job():
    from .swarm import sweep
    return await sweep()


async def _swarm_housekeeping_job():
    from .swarm import housekeeping_sweep
    return await housekeeping_sweep()


async def _warm_caches_job():
    from .graph import warm_caches
    from .state import proposed_focus
    import asyncio
    # Warm MCP caches and proposed_focus in parallel; errors are suppressed internally.
    await asyncio.gather(warm_caches(), proposed_focus(), return_exceptions=True)


# Jobs that must run every day, not just on active_days: monitoring/health
# probes need to keep tracing the pipeline over weekends too. (The companion's
# reply loop runs outside the scheduler; its proactive side stays weekday-gated
# except it should still catch weekend fires, so keep it always-on.)
_ALWAYS_ON = {"osca_monitor", "companion_proactive", "graph_reauth"}


def _wrap(fn, job_id: str):
    async def runner():
        apply_schedules()  # hot-reload policy.yaml each tick
        from datetime import datetime
        active = policy()["schedules"].get("active_days", ["mon", "tue", "wed", "thu", "fri"])
        if job_id not in _ALWAYS_ON and datetime.now().strftime("%a").lower()[:3] not in active:
            return
        from .health import tracked
        try:
            await tracked(job_id, fn)
        except Exception as e:
            log.exception("job %s failed", job_id)
            db.audit("job_error", {"job": job_id, "error": str(e)[:500]})
    return runner


async def run_startup_jobs():
    """One-off checks at app boot.

    - osca_monitor: always (the pipeline monitor shouldn't wait out its first
      5-min interval after a restart — resume tracing immediately).
    - graph_reauth: always (a restart is exactly when the user is watching;
      don't make them wait out the 10-min interval if the token's already dead).
    - ensure_daily_note: weekday-gated (safety net for when the 08:00 brief
      never ran because the app was down)."""
    from datetime import datetime

    from .health import tracked

    try:
        await tracked("osca_monitor", _osca_monitor_job, trigger="startup")
    except Exception as e:
        log.exception("startup osca_monitor failed")
        db.audit("job_error", {"job": "osca_monitor.startup", "error": str(e)[:500]})

    try:
        await tracked("graph_reauth", _graph_reauth_job, trigger="startup")
    except Exception as e:
        log.exception("startup graph_reauth failed")
        db.audit("job_error", {"job": "graph_reauth.startup", "error": str(e)[:500]})

    # swarm GitHub sync must not wait out its first interval after a restart —
    # merges/comments that landed while the app was down get picked up now
    try:
        await tracked("swarm_housekeeping", _swarm_housekeeping_job, trigger="startup")
    except Exception as e:
        log.exception("startup swarm_housekeeping failed")
        db.audit("job_error", {"job": "swarm_housekeeping.startup", "error": str(e)[:500]})

    active = policy()["schedules"].get("active_days", ["mon", "tue", "wed", "thu", "fri"])
    if datetime.now().strftime("%a").lower()[:3] not in active:
        return
    from .daily import ensure_daily_note
    try:
        await tracked("ensure_daily_note", ensure_daily_note, trigger="startup")
    except Exception as e:
        log.exception("startup ensure_daily_note failed")
        db.audit("job_error", {"job": "ensure_daily_note.startup", "error": str(e)[:500]})


def start():
    apply_schedules()
    scheduler.start()
