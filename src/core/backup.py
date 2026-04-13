"""
backup.py — Concise core backup logic for RBRCS.
"""
import logging, difflib
from src.utils.ssh import SSHManager
from src.core.database import *
from src.utils.alerts import send_alert

logger = logging.getLogger("rbrcs.backup")
ssh = SSHManager()

def backup_router(rid, ctype="auto"):
    r = get_router(rid)
    if not r: return {"success": False, "message": "Router not found"}
    logger.info(f"Backing up: {r['name']} ({r['host']})")

    try:
        text = ssh.fetch_config(r)
        if not text or len(text.strip()) < 10: return {"success": False, "message": "Empty config"}

        # Corruption & Factory Check
        from src.core.golden import check_corruption
        from src.core.restore import is_factory_default, restore_router
        
        is_corrupt, reason = check_corruption(text, r.get("device_type"))
        if is_corrupt:
            log_event(rid, "config_corrupted", reason, "error")
            send_alert(r, "config_corrupted", f"⚠️ Corrupt on {r['name']}: {reason}")
            return {"success": False, "message": f"Corrupt: {reason}"}

        if is_factory_default(text, r.get("device_type")):
            log_event(rid, "factory_reset", "Triggering auto-restore", "warning")
            send_alert(r, "factory_reset", f"🚨 Factory reset on {r['name']}. Restoring.")
            return restore_router(rid)

        # Store & Diff
        cid, is_new = store_config(rid, text, ctype)
        update_router_status(rid, "online")
        
        msg = f"Saved (ID: {cid})" if is_new else "Unchanged"
        if is_new:
            hist = get_config_history(rid, limit=2)
            if len(hist) >= 2:
                prev = get_config(cid=hist[1]["id"])
                if prev:
                    d = list(difflib.unified_diff(prev["config_text"].splitlines(), text.splitlines(), n=0))
                    msg += f" | Δ +{sum(1 for l in d if l.startswith('+') and not l.startswith('+++'))}/-{sum(1 for l in d if l.startswith('-') and not l.startswith('---'))}"
            
            # Drift Check
            from src.core.golden import get_golden_config, check_drift, set_golden_config
            if get_golden_config(rid):
                drift = check_drift(rid, text, r.get("device_type"))
                if drift and drift["has_drift"]: send_alert(r, "config_drift", f"⚠️ {r['name']}: {drift['summary']}")
            else: set_golden_config(rid, text, "auto-first")

        return {"success": True, "message": msg, "is_new": is_new, "config_id": cid}

    except Exception as e:
        logger.error(f"Backup failed: {e}")
        update_router_status(rid, "error")
        log_event(rid, "backup_error", str(e), "error")
        return {"success": False, "message": str(e)}

def backup_all(): return [backup_router(r["id"]) for r in get_all_routers()]
