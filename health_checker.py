"""
health_checker.py — Periodic health checks and fallback polling.

SCENARIOS HANDLED:
  1. Router online and healthy              → backup if needed
  2. Router went offline                    → log + alert
  3. Router came back online (was offline)  → check for factory reset, auto-restore
  4. Router reachable but SSH fails         → log ssh_error, alert
  5. Router was unknown state at startup    → treat as potential reset, check config
  6. Router stays offline multiple checks   → do NOT spam alerts (only alert once)
  7. Router comes back with good config     → just log online, no restore
  8. Router comes back with factory config  → auto-restore triggered
"""

import os
import logging
from ssh_manager import SSHManager
from database import get_all_routers, get_router, update_router_status, log_event
from backup_engine import backup_router
from restore_engine import check_and_auto_restore

logger = logging.getLogger("rbrcs.health")
ssh = SSHManager()

# Track which routers have already had an offline alert sent this cycle
# to avoid spamming alerts on every health check interval
_offline_alerted = set()


def check_all_routers(db_path=None):
    """
    Check all registered routers.
    Returns list of result dicts.
    """
    routers = get_all_routers(db_path)
    results = []

    for router in routers:
        result = check_single_router(router, db_path)
        results.append(result)

    online = sum(1 for r in results if r["status"] == "online")
    offline = sum(1 for r in results if r["status"] == "offline")
    errors = sum(1 for r in results if r["status"] == "ssh_error")

    logger.info(
        f"Health check complete: {online} online, {offline} offline, {errors} ssh_error "
        f"(total: {len(results)})"
    )
    return results


def check_single_router(router, db_path=None):
    """
    Full health check for one router:
      1. TCP port check (is SSH port open?)
      2. SSH authentication test
      3. If just came back online → check for factory reset
      4. If factory reset found → auto-restore
    """
    router_id = router["id"]
    previous_status = router.get("status", "unknown")

    # ── Step 1: TCP ping (fast, no SSH overhead) ───────────
    is_reachable = ssh.ping(router)

    if not is_reachable:
        # Only log and alert the FIRST time it goes offline (not on every check)
        if previous_status != "offline":
            logger.warning(f"{router['name']}: Router went OFFLINE (host: {router['host']})")
            log_event(router_id, "status_offline",
                      f"Router {router['host']} is unreachable on port {router.get('port', 22)}",
                      "warning", db_path)
            _send_alert(router, "offline",
                        f"⚠️ Router OFFLINE: {router['name']} ({router['host']}) is unreachable.")
            _offline_alerted.add(router_id)

        update_router_status(router_id, "offline", db_path)
        return {"router_id": router_id, "name": router["name"], "status": "offline"}

    # ── Step 2: SSH authentication test ───────────────────
    ssh_ok, ssh_msg = ssh.test_connection(router)

    if not ssh_ok:
        logger.warning(f"{router['name']}: Reachable but SSH failed — {ssh_msg}")
        update_router_status(router_id, "ssh_error", db_path)

        # Only log ssh_error once per cycle too
        if previous_status != "ssh_error":
            log_event(router_id, "ssh_error",
                      f"SSH authentication failed: {ssh_msg}", "warning", db_path)
            _send_alert(router, "ssh_error",
                        f"⚠️ SSH ERROR: Cannot log into {router['name']} ({router['host']}). "
                        f"Check credentials. Error: {ssh_msg}")

        return {"router_id": router_id, "name": router["name"], "status": "ssh_error"}

    # ── Step 3: Router is online and SSH is good ──────────
    update_router_status(router_id, "online", db_path)

    # Clear offline alert state since it's back
    _offline_alerted.discard(router_id)

    # ── Step 4: Router just came back — check for factory reset ──
    # Covers: power restored, reboot after reset, SSH was temporarily down
    came_back = previous_status in ("offline", "unknown", "ssh_error")

    if came_back:
        logger.info(f"{router['name']}: Router is BACK ONLINE (was: {previous_status})")
        log_event(router_id, "status_online",
                  f"Router back online after status '{previous_status}'",
                  "info", db_path)

        # Run factory reset detection + auto-restore if needed
        reset_check = check_and_auto_restore(router_id, db_path)

        if reset_check["was_reset"]:
            restore_result = reset_check.get("restore_result", {})
            if restore_result and restore_result.get("success"):
                logger.warning(f"{router['name']}: Factory reset detected and RESTORED successfully")
                return {"router_id": router_id, "name": router["name"],
                        "status": "online", "restored": True}
            else:
                logger.error(f"{router['name']}: Factory reset detected but RESTORE FAILED")
                return {"router_id": router_id, "name": router["name"],
                        "status": "online", "restored": False, "restore_failed": True}
        else:
            logger.info(f"{router['name']}: Config looks normal — no restore needed")

    return {"router_id": router_id, "name": router["name"], "status": "online"}


def poll_backup_all(db_path=None):
    """
    Fallback polling: back up all currently-online routers.
    Called on a schedule (default every 30 min) when syslog is not available.

    Note: backup_engine.py checks for factory default BEFORE saving,
    so even if this runs right after a reset, it won't overwrite good backups
    with factory config.
    """
    routers = get_all_routers(db_path)
    results = []

    for router in routers:
        status = router.get("status", "unknown")
        if status == "online":
            result = backup_router(router["id"], "poll", db_path)
            results.append(result)
        else:
            logger.debug(f"Skipping {router['name']} — status: {status}")

    backed_up = sum(1 for r in results if r.get("is_new"))
    logger.info(
        f"Poll backup complete: {backed_up} new backup(s) from {len(results)} online routers"
    )
    return results


def _send_alert(router, event_type, message):
    """Send webhook alert if configured."""
    try:
        import yaml, requests
        with open("config.yaml", "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(os.path.expandvars(f.read()))
        webhook_url = cfg.get("alerts", {}).get("webhook_url", "")
        if webhook_url:
            requests.post(webhook_url, json={"text": message}, timeout=5)
    except Exception as e:
        logger.debug(f"Alert not sent ({event_type}): {e}")
