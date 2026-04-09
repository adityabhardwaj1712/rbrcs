"""
backup_engine.py — Core backup logic (Production-Grade).

SOLVES ALL 12 PROBLEMS:
  #1  Factory Reset        → is_factory_default() blocks save + auto-restore
  #2  Power Failure        → regular polling catches unsaved changes
  #3  Human Error          → golden config drift detection on every backup
  #4  Unauthorized Access  → drift alert shows EXACTLY what changed
  #5  Config Overwrite     → golden config never overwritten by bad push
  #6  Partial Config Loss  → critical section check vs golden
  #7  Firmware Upgrade     → factory detection catches post-upgrade resets
  #8  Corrupted Config     → corruption detector rejects garbled configs
  #9  IP Loss              → handled by health_checker retry logic
  #10 Network Disconnection→ handled by health_checker offline detection
  #11 Multi-Router Mgmt    → per-router golden + per-router schedules
  #12 No Backup Available  → first successful backup auto-sets golden
"""

import logging
from ssh_manager import SSHManager
from database import (
    store_config, get_router, get_all_routers,
    update_router_status, log_event
)
from alerts import send_alert as _send_alert

logger = logging.getLogger("rbrcs.backup")
ssh = SSHManager()


def backup_router(router_id, change_type="auto", db_path=None):
    """
    Back up a single router's configuration.
    Returns dict with keys: success, router_id, message, is_new, config_id
    """
    router = get_router(router_id, db_path)
    if not router:
        return {"success": False, "router_id": router_id,
                "message": "Router not found", "is_new": False, "config_id": None}

    logger.info(f"Backing up: {router['name']} ({router['host']})")

    try:
        # ── Step 1: Fetch config via SSH ──────────────────────
        config_text = ssh.fetch_config(router)

        # ── Step 2: Reject empty configs ──────────────────────
        if not config_text or len(config_text.strip()) < 10:
            msg = "Empty or invalid config received"
            logger.warning(f"{router['name']}: {msg}")
            log_event(router_id, "backup_warning", msg, "warning", db_path)
            update_router_status(router_id, "online", db_path)
            return {"success": False, "router_id": router_id,
                    "message": msg, "is_new": False, "config_id": None}

        # ── Step 3: Corruption check ──────────────────────────
        from golden_config import check_corruption
        is_corrupt, corrupt_reason = check_corruption(config_text, router.get("device_type"))
        if is_corrupt:
            msg = f"Corrupted config detected: {corrupt_reason}"
            logger.error(f"{router['name']}: {msg}")
            log_event(router_id, "config_corrupted", msg, "error", db_path)
            _send_alert(router, "config_corrupted",
                        f"⚠️ CORRUPTED CONFIG on {router['name']}: {corrupt_reason}")
            update_router_status(router_id, "online", db_path)
            return {"success": False, "router_id": router_id,
                    "message": msg, "is_new": False, "config_id": None}

        # ── Step 4: Factory reset check ───────────────────────
        from restore_engine import is_factory_default, restore_router
        if is_factory_default(config_text, router.get("device_type")):
            msg = "Factory default config detected — triggering auto-restore"
            logger.warning(f"{router['name']}: {msg}")
            log_event(router_id, "factory_reset_detected", msg, "warning", db_path)
            _send_alert(router, "factory_reset_detected",
                        f"🚨 Factory reset on {router['name']} ({router['host']}). Auto-restoring.")

            restore_result = restore_router(router_id, db_path=db_path)
            restore_msg = restore_result.get("message", "unknown")
            if not restore_result.get("success"):
                _send_alert(router, "restore_failed",
                            f"❌ Auto-restore FAILED on {router['name']}: {restore_msg}")

            return {"success": False, "router_id": router_id,
                    "message": f"Factory reset intercepted — {restore_msg}",
                    "is_new": False, "config_id": None}

        # ── Step 5: Store config (with dedup) ─────────────────
        config_id, is_new = store_config(router_id, config_text, change_type, db_path)
        update_router_status(router_id, "online", db_path)

        if is_new:
            msg = f"New config saved (ID: {config_id}, {len(config_text)} bytes)"

            # Diff summary vs previous backup
            try:
                from database import get_config_history, get_config_by_id
                import difflib
                history = get_config_history(router_id, limit=2, db_path=db_path)
                if len(history) >= 2:
                    prev = get_config_by_id(history[1]["id"], db_path)
                    if prev:
                        d = list(difflib.unified_diff(
                            prev["config_text"].splitlines(),
                            config_text.splitlines(), n=0))
                        adds = sum(1 for l in d if l.startswith('+') and not l.startswith('+++'))
                        dels = sum(1 for l in d if l.startswith('-') and not l.startswith('---'))
                        msg += f" | Δ +{adds}/-{dels} lines"
            except Exception:
                pass

            logger.info(f"{router['name']}: {msg}")
            log_event(router_id, "backup_new", msg, "info", db_path)

            # ── Step 6: Golden config drift check ─────────────
            from golden_config import check_drift, get_golden_config, set_golden_config
            golden = get_golden_config(router_id, db_path)
            if golden:
                drift = check_drift(router_id, config_text,
                                    router.get("device_type"), db_path)
                if drift and drift["has_drift"]:
                    drift_msg = f"Config drift: {drift['summary']}"
                    severity = "error" if drift["missing_sections"] else "warning"
                    _send_alert(router, "config_drift",
                                f"⚠️ {router['name']}: {drift_msg}")
            else:
                # No golden config exists yet → auto-promote first good backup
                set_golden_config(router_id, config_text, "auto-first-backup", db_path)
                logger.info(f"{router['name']}: First backup auto-promoted as golden config")

            # ── Step 7: Compliance scan ───────────────────────
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
    """Back up all registered routers."""
    routers = get_all_routers(db_path)
    return [backup_router(r["id"], "auto", db_path) for r in routers]

