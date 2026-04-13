"""
scheduler.py — Concise APScheduler orchestration for RBRCS.
"""
import logging
from apscheduler.schedulers.background import BackgroundScheduler
from src.core.health import check_all_routers
from src.core.backup import backup_router, backup_all
from src.core.retention import run_retention_cleanup
from src.utils.ssh import SSHManager
from src.core.golden import check_drift, get_golden_config
from src.core.database import get_all_routers

logger = logging.getLogger("rbrcs.scheduler")
sched = BackgroundScheduler(daemon=True)

def _drift():
    ssh = SSHManager()
    for r in get_all_routers():
        if r.get("status") == "online" and get_golden_config(r["id"]):
            try:
                curr = ssh.fetch_config(r)
                if curr: check_drift(r["id"], curr, r["device_type"])
            except: pass

def start_scheduler(cfg):
    h_int = cfg.get("health_check", {}).get("interval_minutes", 10)
    b_int = cfg.get("health_check", {}).get("backup_interval_minutes", 30)
    cl_hr = cfg.get("retention", {}).get("cleanup_hour", 3)

    sched.add_job(check_all_routers, "interval", minutes=h_int, id="health")
    sched.add_job(_drift, "interval", hours=1, id="drift")
    sched.add_job(run_retention_cleanup, "cron", hour=cl_hr, args=[cfg.get("retention")], id="cleanup")
    
    for r in cfg.get("routers", []):
        ri = r.get("backup_interval_minutes", b_int)
        sched.add_job(backup_router, "interval", minutes=int(ri), args=[r["id"], "poll"], id=f"bkp_{r['id']}")

    sched.start(); logger.info("Scheduler online")

def stop_scheduler(): sched.shutdown(wait=False)
def get_scheduled_jobs(): return [{"id": j.id, "name": j.name, "next": str(j.next_run_time)} for j in sched.get_jobs()]
