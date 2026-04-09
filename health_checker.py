"""
health_checker.py — Periodic health checks and fallback polling.

Checks router reachability, triggers backups, and detects factory resets.
"""

import logging
from ssh_manager import SSHManager
from database import get_all_routers, update_router_status, log_event
from backup_engine import backup_router
from restore_engine import check_and_auto_restore

logger = logging.getLogger("rbrcs.health")
ssh = SSHManager()


def check_all_routers(db_path=None):
    """
    Check the status of all registered routers.
    Updates their online/offline status in the database.
    """
    routers = get_all_routers(db_path)
    results = []

    for router in routers:
        result = check_single_router(router, db_path)
        results.append(result)

    online = sum(1 for r in results if r["status"] == "online")
    logger.info(f"Health check complete: {online}/{len(results)} routers online")
    return results


def check_single_router(router, db_path=None):
    """
    Check a single router's health:
      1. TCP ping to SSH port
      2. If reachable, verify SSH login
      3. If was offline then came back, check for factory reset
    """
    router_id = router["id"]
    previous_status = router.get("status", "unknown")

    # Step 1: Quick TCP ping
    is_reachable = ssh.ping(router)

    if not is_reachable:
        if previous_status != "offline":
            logger.warning(f"{router['name']}: Router went OFFLINE")
            log_event(router_id, "status_offline",
                      f"Router {router['host']} is unreachable", "warning", db_path)
        update_router_status(router_id, "offline", db_path)
        return {"router_id": router_id, "name": router["name"], "status": "offline"}

    # Step 2: SSH connection test
    success, message = ssh.test_connection(router)

    if not success:
        logger.warning(f"{router['name']}: Reachable but SSH failed — {message}")
        update_router_status(router_id, "ssh_error", db_path)
        log_event(router_id, "ssh_error", message, "warning", db_path)
        return {"router_id": router_id, "name": router["name"], "status": "ssh_error"}

    # Step 3: Router is online
    update_router_status(router_id, "online", db_path)

    # If router just came back from offline, check for factory reset
    if previous_status in ("offline", "unknown"):
        logger.info(f"{router['name']}: Router came ONLINE (was {previous_status})")
        log_event(router_id, "status_online",
                  f"Router is back online (was {previous_status})", "info", db_path)

        # Check for factory reset and auto-restore
        reset_check = check_and_auto_restore(router_id, db_path)
        if reset_check["was_reset"]:
            logger.warning(f"{router['name']}: Factory reset detected and auto-restored!")

    return {"router_id": router_id, "name": router["name"], "status": "online"}


def poll_backup_all(db_path=None):
    """
    Fallback polling: back up all online routers.
    Called periodically when syslog is not available.
    """
    routers = get_all_routers(db_path)
    results = []

    for router in routers:
        if router.get("status") == "online":
            result = backup_router(router["id"], "poll", db_path)
            results.append(result)
        else:
            logger.debug(f"Skipping offline router: {router['name']}")

    backed_up = sum(1 for r in results if r.get("is_new"))
    logger.info(f"Poll backup complete: {backed_up} new backups from {len(results)} routers")
    return results
