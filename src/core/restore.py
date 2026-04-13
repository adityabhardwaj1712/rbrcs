"""
restore.py — Concise core restore logic for RBRCS.
"""
import logging
from src.utils.ssh import SSHManager
from src.core.database import *
from src.utils.alerts import send_alert

logger = logging.getLogger("rbrcs.restore")
ssh = SSHManager()

HINTS = {
    "cisco_ios": {"len": 20, "kw": ["interface", "ip address", "username"], "host": ["Router", "Switch"]},
    "mikrotik_routeros": {"len": 10, "kw": ["/ip", "/interface"], "host": []},
}

def is_factory_default(text, dtype):
    if not text or len(text.strip()) < 10: return True
    h = HINTS.get(dtype, HINTS["cisco_ios"])
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if len(lines) < h["len"]: return True
    if any(any(f"hostname {x}" in l for x in h["host"]) for l in lines): return True
    if h["kw"] and not any(kw in text.lower() for kw in h["kw"]): return True
    return False

def restore_router(rid, cid=None):
    r = get_router(rid)
    if not r: return {"success": False, "message": "Router not found"}
    
    cfg = None; src = ""
    if cid: 
        cfg = get_config(cid=cid); src = f"backup #{cid}"
    else:
        from src.core.golden import get_golden_config
        cfg = get_golden_config(rid); src = "golden"
        if not cfg:
            hist = get_config_history(rid, limit=10)
            for h in hist:
                c = get_config(cid=h["id"])
                if c and not is_factory_default(c["config_text"], r.get("device_type")): 
                    cfg = c; src = f"last good #{c['id']}"; break
    
    if not cfg: return {"success": False, "message": "No valid config source found"}
    logger.info(f"Restoring {r['name']} from {src}")

    try:
        ok, msg = ssh.push_config(r, cfg["config_text"])
        if ok:
            log_event(rid, "restore_success", f"From {src}: {msg}")
            send_alert(r, "restore_success", f"✅ Restore SUCCESS on {r['name']}")
        else:
            log_event(rid, "restore_error", msg, "error")
            send_alert(r, "restore_failed", f"❌ Restore FAILED on {r['name']}: {msg}")
        return {"success": ok, "message": msg}
    except Exception as e:
        return {"success": False, "message": str(e)}

def check_and_restore(rid):
    r = get_router(rid)
    if not r: return
    try:
        if is_factory_default(ssh.fetch_config(r), r["device_type"]):
            logger.warning(f"{r['name']}: Factory reset detected!")
            return restore_router(rid)
    except: pass
