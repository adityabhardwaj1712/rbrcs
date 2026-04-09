"""
backup_engine.py — Core backup logic.

Fetches router config via SSH, hashes it, and stores only if changed.

SCENARIOS HANDLED:
  1. Normal backup — config saved if changed
  2. Factory reset detected during poll (router stayed online) — restore triggered
  3. Empty/invalid config — warning logged, no save
  4. SSH failure — error logged, status set to error
"""

import os
import logging
from ssh_manager import SSHManager
from database import (
    store_config, get_router, get_all_routers,
    update_router_status, log_event
)

logger = logging.getLogger("rbrcs.backup")
ssh = SSHManager()


def backup_router(router_id, change_type="auto", db_path=None):
    """
    Back up a single router's configuration.

    Returns:
        dict with keys: success, router_id, message, is_new, config_id
    """
    router = get_router(router_id, db_path)
    if not router:
        return {"success": False, "router_id": router_id,
                "message": "Router not found", "is_new": False, "config_id": None}

    logger.info(f"Backing up router: {router['name']} ({router['host']})")

    try:
        # ── Step 1: Fetch config via SSH ──────────────────────
        config_text = ssh.fetch_config(router)

        # ── Step 2: Validate — reject empty configs ───────────
        if not config_text or len(config_text.strip()) < 10:
            msg = "Empty or invalid config received"
            logger.warning(f"{router['name']}: {msg}")
            log_event(router_id, "backup_warning", msg, "warning", db_path)
            update_router_status(router_id, "online", db_path)
            return {"success": False, "router_id": router_id,
                    "message": msg, "is_new": False, "config_id": None}

        # ── Step 3: Factory reset check (catches remote/online resets) ──
        # This handles the case where the router was reset WITHOUT going offline.
        # Without this check, a factory config would be saved as a new backup,
        # overwriting the good config chain.
        from restore_engine import is_factory_default, restore_router
        if is_factory_default(config_text, router.get("device_type")):
            msg = "Factory default config detected during backup poll — triggering auto-restore"
            logger.warning(f"{router['name']}: {msg}")
            log_event(router_id, "factory_reset_detected", msg, "warning", db_path)

            # Send alert
            _send_alert(router, "factory_reset_detected",
                        f"Factory reset detected on {router['name']} ({router['host']}) during backup poll. Auto-restore triggered.")

            # Trigger restore from last known good backup
            restore_result = restore_router(router_id, db_path=db_path)
            restore_msg = restore_result.get("message", "unknown")

            if restore_result.get("success"):
                logger.info(f"{router['name']}: Auto-restore SUCCESS — {restore_msg}")
            else:
                logger.error(f"{router['name']}: Auto-restore FAILED — {restore_msg}")
                _send_alert(router, "restore_failed",
                            f"Auto-restore FAILED on {router['name']}. Manual intervention required! Reason: {restore_msg}")

            return {"success": False, "router_id": router_id,
                    "message": f"Factory reset intercepted — restore result: {restore_msg}",
                    "is_new": False, "config_id": None}

        # ── Step 4: Store config (with dedup) ─────────────────
        config_id, is_new = store_config(router_id, config_text, change_type, db_path)
        update_router_status(router_id, "online", db_path)

        if is_new:
            msg = f"New config saved (ID: {config_id}, size: {len(config_text)} bytes)"

            # Generate diff summary vs previous backup
            try:
                from database import get_config_history, get_config_by_id
                import difflib
                history = get_config_history(router_id, limit=2, db_path=db_path)
                if len(history) >= 2:
                    prev_config = get_config_by_id(history[1]["id"], db_path)
                    if prev_config:
                        prev_txt = prev_config["config_text"].splitlines()
                        curr_txt = config_text.splitlines()
                        diff_lines = list(difflib.unified_diff(prev_txt, curr_txt, n=0))
                        additions = sum(1 for l in diff_lines if l.startswith('+') and not l.startswith('+++'))
                        deletions = sum(1 for l in diff_lines if l.startswith('-') and not l.startswith('---'))
                        msg += f" | Changes: +{additions} lines / -{deletions} lines"
            except Exception as e:
                logger.debug(f"Diff summary skipped: {e}")

            logger.info(f"{router['name']}: {msg}")
            log_event(router_id, "backup_new", msg, "info", db_path)

            # Run compliance scanner
            from compliance import run_compliance_check
            run_compliance_check(router_id, router.get("device_type"), config_text, db_path)

        else:
            msg = "Config unchanged — skipped (dedup)"
            logger.debug(f"{router['name']}: {msg}")

        return {"success": True, "router_id": router_id,
                "message": msg, "is_new": is_new, "config_id": config_id}

    except Exception as e:
        msg = f"Backup failed: {str(e)}"
        logger.error(f"{router['name']}: {msg}")
        update_router_status(router_id, "error", db_path)
        log_event(router_id, "backup_error", msg, "error", db_path)
        return {"success": False, "router_id": router_id,
                "message": msg, "is_new": False, "config_id": None}


def backup_all(db_path=None):
    """Back up all registered routers. Returns list of results."""
    routers = get_all_routers(db_path)
    results = []
    for router in routers:
        result = backup_router(router["id"], "auto", db_path)
        results.append(result)
    return results


def _send_alert(router, event_type, message):
    """Send webhook alert if configured."""
    try:
        import yaml, requests
        with open("config.yaml", "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(os.path.expandvars(f.read()))
        webhook_url = cfg.get("alerts", {}).get("webhook_url", "")
        if webhook_url:
            requests.post(webhook_url, json={"text": f"🚨 RBRCS ALERT: {message}"},
                          timeout=5)
    except Exception as e:
        logger.debug(f"Alert not sent: {e}")
