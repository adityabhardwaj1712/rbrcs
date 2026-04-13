"""
health.py — Concise health tracking for RBRCS.
"""
import time, logging
from src.core.database import *
from src.core.backup import backup_router
from src.core.restore import check_and_restore
from src.utils.ssh import SSHManager
from src.utils.alerts import send_alert

logger = logging.getLogger("rbrcs.health")
ssh = SSHManager(); _state = {} # rid -> {"retries": 0, "status": "unknown"}

def check_single_router(r):
    rid = r["id"]; prev = r.get("status", "unknown"); s = _state.setdefault(rid, {"retries": 0})
    
    if not ssh.ping(r):
        s["retries"] += 1
        if s["retries"] >= 3 and prev != "offline":
            log_event(rid, "status_offline", "Unreachable after 3 retries", "warning")
            send_alert(r, "offline", f"⚠️ OFFLINE: {r['name']} ({r['host']})")
            update_router_status(rid, "offline")
        return {"id": rid, "status": "offline" if s["retries"] >= 3 else prev}

    s["retries"] = 0
    ok, msg = ssh.test_connection(r)
    if not ok:
        if prev != "ssh_error":
            log_event(rid, "ssh_error", msg, "warning"); send_alert(r, "ssh_error", f"⚠️ SSH Fail: {r['name']}")
            update_router_status(rid, "ssh_error")
        return {"id": rid, "status": "ssh_error"}

    update_router_status(rid, "online")
    if prev in ("offline", "ssh_error"):
        log_event(rid, "status_online", "Back online", "info"); send_alert(r, "back_online", f"✅ ONLINE: {r['name']}")
        check_and_restore(rid)
    return {"id": rid, "status": "online"}

def check_all_routers(): return [check_single_router(r) for r in get_all_routers()]
