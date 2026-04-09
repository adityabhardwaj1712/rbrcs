"""
health_checker.py — Periodic health checks with retry + exponential backoff.

SOLVES:
  #9  IP Loss / Unreachable  → retry with backoff (3 attempts before marking offline)
  #10 Network Disconnection  → same retry logic, ONE alert per event
  #2  Power Failure          → detects router coming back online, triggers reset check

Ultra-lightweight: no extra threads, no timers — just state tracking in dicts.
"""

import os
import time
import logging
from ssh_manager import SSHManager
from database import get_all_routers, update_router_status, log_event
from backup_engine import backup_router
from restore_engine import check_and_auto_restore

logger = logging.getLogger("rbrcs.health")
ssh = SSHManager()

# ── State tracking (in-memory, zero storage cost) ──────────
_offline_alerted = set()           # routers that already got an offline alert
_retry_count = {}                  # router_id → consecutive failure count
_first_offline_time = {}           # router_id → timestamp when first went offline

MAX_RETRIES = 3                    # attempts before marking offline
LONG_OFFLINE_HOURS = 2             # alert again if offline > this long


def check_all_routers(db_path=None):
    """Check all registered routers. Returns list of result dicts."""
    routers = get_all_routers(db_path)
    results = [check_single_router(r, db_path) for r in routers]

    online = sum(1 for r in results if r["status"] == "online")
    offline = sum(1 for r in results if r["status"] == "offline")
    logger.info(f"Health check: {online} online, {offline} offline (total: {len(results)})")
    return results


def check_single_router(router, db_path=None):
    """
    Full health check with retry logic:
      1. TCP ping (fast) — if fails, increment retry counter
      2. If retries < MAX_RETRIES → don't mark offline yet (transient issue)
      3. If retries >= MAX_RETRIES → mark offline, send ONE alert
      4. If router comes back → check for factory reset
    """
    router_id = router["id"]
    previous_status = router.get("status", "unknown")

    # ── Step 1: TCP ping ──────────────────────────────────
    is_reachable = ssh.ping(router)

    if not is_reachable:
        # Increment retry counter
        _retry_count[router_id] = _retry_count.get(router_id, 0) + 1
        retries = _retry_count[router_id]

        if retries < MAX_RETRIES:
            # Transient failure — don't alert yet
            logger.debug(f"{router['name']}: Unreachable (retry {retries}/{MAX_RETRIES})")
            return {"router_id": router_id, "name": router["name"],
                    "status": previous_status, "retrying": True}

        # Confirmed offline after MAX_RETRIES attempts
        if previous_status != "offline":
            logger.warning(f"{router['name']}: OFFLINE after {retries} retries")
            log_event(router_id, "status_offline",
                      f"Unreachable on {router['host']}:{router.get('port', 22)} "
                      f"after {retries} retries", "warning", db_path)
            _send_alert(router, "offline",
                        f"⚠️ OFFLINE: {router['name']} ({router['host']}) unreachable")
            _offline_alerted.add(router_id)
            _first_offline_time[router_id] = time.time()

        # Check if long-offline alert is needed
        elif router_id in _first_offline_time:
            hours_offline = (time.time() - _first_offline_time[router_id]) / 3600
            if hours_offline > LONG_OFFLINE_HOURS and router_id in _offline_alerted:
                # Send periodic reminder (every LONG_OFFLINE_HOURS)
                _send_alert(router, "long_offline",
                            f"🔴 STILL OFFLINE: {router['name']} has been down "
                            f"for {hours_offline:.1f} hours. Check physically!")
                _first_offline_time[router_id] = time.time()  # Reset timer

        update_router_status(router_id, "offline", db_path)
        return {"router_id": router_id, "name": router["name"], "status": "offline"}

    # ── Router is reachable — reset retry counter ─────────
    _retry_count[router_id] = 0

    # ── Step 2: SSH authentication test ───────────────────
    ssh_ok, ssh_msg = ssh.test_connection(router)

    if not ssh_ok:
        logger.warning(f"{router['name']}: Reachable but SSH failed — {ssh_msg}")
        update_router_status(router_id, "ssh_error", db_path)

        if previous_status != "ssh_error":
            log_event(router_id, "ssh_error",
                      f"SSH failed: {ssh_msg}", "warning", db_path)
            _send_alert(router, "ssh_error",
                        f"⚠️ SSH ERROR on {router['name']}: {ssh_msg}")

        return {"router_id": router_id, "name": router["name"], "status": "ssh_error"}

    # ── Step 3: Router is online ──────────────────────────
    update_router_status(router_id, "online", db_path)
    _offline_alerted.discard(router_id)
    _first_offline_time.pop(router_id, None)

    # ── Step 4: Came back from offline/error → check for reset ──
    came_back = previous_status in ("offline", "unknown", "ssh_error")
    if came_back:
        logger.info(f"{router['name']}: BACK ONLINE (was: {previous_status})")
        log_event(router_id, "status_online",
                  f"Back online from '{previous_status}'", "info", db_path)
        _send_alert(router, "back_online",
                    f"✅ ONLINE: {router['name']} ({router['host']}) is back!")

        # Check for factory reset
        reset_check = check_and_auto_restore(router_id, db_path)
        if reset_check["was_reset"]:
            result = reset_check.get("restore_result", {})
            restored = result.get("success", False) if result else False
            return {"router_id": router_id, "name": router["name"],
                    "status": "online", "restored": restored}

    return {"router_id": router_id, "name": router["name"], "status": "online"}


def poll_backup_all(db_path=None):
    """Fallback polling: back up all online routers."""
    routers = get_all_routers(db_path)
    results = []
    for router in routers:
        if router.get("status") == "online":
            results.append(backup_router(router["id"], "poll", db_path))
    backed_up = sum(1 for r in results if r.get("is_new"))
    logger.info(f"Poll backup: {backed_up} new from {len(results)} online routers")
    return results


def _send_alert(router, event_type, message):
    """Send webhook alert if configured."""
    try:
        import yaml, requests
        with open("config.yaml", "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(os.path.expandvars(f.read()))
        url = cfg.get("alerts", {}).get("webhook_url", "")
        if url:
            requests.post(url, json={"text": message}, timeout=5)
    except Exception:
        pass
