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
    from .daily import evening_shutdown, morning_brief

    jobs = {
        "teams_sweep": (teams_sweep.sweep, IntervalTrigger(minutes=sched["teams_sweep_minutes"])),
        "email_sweep": (email_sweep.sweep, IntervalTrigger(minutes=sched["email_sweep_minutes"])),
        "open_loops": (loops_sweep.sweep, IntervalTrigger(minutes=sched["open_loops_minutes"])),
        "transcripts": (transcripts_sweep.sweep,
                        IntervalTrigger(minutes=sched.get("transcripts_sweep_minutes", 15))),
        "pr_review": (pr_review_sweep.sweep,
                      IntervalTrigger(minutes=policy().get("pr_review", {}).get("sweep_minutes", 5))),
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
        "morning_brief": (morning_brief, _cron(sched["morning_brief"])),
        "evening_shutdown": (evening_shutdown, _cron(sched["evening_shutdown"])),
        "weekly_status": (_weekly_status_job, _cron_dow(
            policy().get("weekly_status", {}).get("time", "17:00"),
            policy().get("weekly_status", {}).get("day", "fri"))),
    }
    for job_id, (fn, trigger) in jobs.items():
        scheduler.add_job(_wrap(fn, job_id), trigger, id=job_id, replace_existing=True,
                          coalesce=True, max_instances=1)
    _applied = sched
    log.info("schedules applied: %s", sched)


async def _pr_digest_job():
    from .sweeps.github_digest import digest
    return await digest()


async def _chieff_update_job():
    from .sweeps.chieff import daily_update
    return await daily_update()


async def _knowledge_sync_job():
    from .sweeps.knowledge import sync
    return await sync()


async def _pr_status_job():
    from .sweeps.pr_status import sweep
    return await sweep()


async def _pr_readiness_job():
    from .sweeps.pr_readiness import sweep
    return await sweep()


async def _weekly_status_job():
    """Friday: generate the week's draft report (the user approves on the board)."""
    from .sweeps.weekly_status import generate
    return await generate()


def _wrap(fn, job_id: str):
    async def runner():
        apply_schedules()  # hot-reload policy.yaml each tick
        from datetime import datetime
        active = policy()["schedules"].get("active_days", ["mon", "tue", "wed", "thu", "fri"])
        if datetime.now().strftime("%a").lower()[:3] not in active:
            return
        try:
            await fn()
        except Exception as e:
            log.exception("job %s failed", job_id)
            db.audit("job_error", {"job": job_id, "error": str(e)[:500]})
    return runner


def start():
    apply_schedules()
    scheduler.start()
