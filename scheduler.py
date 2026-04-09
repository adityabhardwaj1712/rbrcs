"""
scheduler.py — APScheduler-based job orchestration.

Manages periodic jobs:
  - Health check (every 10 min)
  - Fallback backup (every 30 min)
  - Retention cleanup (daily at 3 AM)
"""

import logging
from apscheduler.schedulers.background import BackgroundScheduler
from health_checker import check_all_routers, poll_backup_all
from retention import run_retention_cleanup
from backup_engine import backup_router

logger = logging.getLogger("rbrcs.scheduler")

scheduler = BackgroundScheduler(daemon=True)


def start_scheduler(config):
    """Start all scheduled jobs based on config."""
    health_interval = config.get("health_check", {}).get("interval_minutes", 10)
    backup_interval = config.get("health_check", {}).get("backup_interval_minutes", 30)
    cleanup_hour = config.get("retention", {}).get("cleanup_hour", 3)
    retention_config = config.get("retention", {})

    # Job 1: Health check — every N minutes
    scheduler.add_job(
        check_all_routers,
        "interval",
        minutes=health_interval,
        id="health_check",
        name="Router Health Check",
        replace_existing=True,
    )
    logger.info(f"Scheduled health check every {health_interval} minutes")

    # Job 2: Per-router fallback backup
    routers = config.get("routers", [])
    if not routers:
        scheduler.add_job(
            poll_backup_all,
            "interval",
            minutes=backup_interval,
            id="poll_backup",
            name="Fallback Config Backup",
            replace_existing=True,
        )
        logger.info(f"Scheduled global fallback backup every {backup_interval} minutes")
    else:
        for r_cfg in routers:
            r_interval = r_cfg.get("backup_interval_minutes", backup_interval)
            scheduler.add_job(
                backup_router,
                "interval",
                minutes=int(r_interval),
                args=[r_cfg["id"], "poll"],
                id=f"backup_{r_cfg['id']}",
                name=f"Backup {r_cfg['name']}",
                replace_existing=True,
            )
            logger.info(f"Scheduled backup for {r_cfg['name']} every {r_interval} minutes")

    # Job 3: Retention cleanup — daily at specified hour
    scheduler.add_job(
        run_retention_cleanup,
        "cron",
        hour=cleanup_hour,
        minute=0,
        args=[retention_config],
        id="retention_cleanup",
        name="Retention Cleanup",
        replace_existing=True,
    )
    logger.info(f"Scheduled retention cleanup daily at {cleanup_hour}:00")

    scheduler.start()
    logger.info("Scheduler started")


def stop_scheduler():
    """Shut down the scheduler."""
    scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped")


def get_scheduled_jobs():
    """Return info about all scheduled jobs."""
    jobs = []
    for job in scheduler.get_jobs():
        jobs.append({
            "id": job.id,
            "name": job.name,
            "next_run": str(job.next_run_time) if job.next_run_time else "N/A",
            "trigger": str(job.trigger),
        })
    return jobs
