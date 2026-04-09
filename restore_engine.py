"""
restore_engine.py — Configuration restore logic.

SCENARIOS HANDLED:
  1. Router came back from offline with factory config  → auto-restore
  2. Router was reset remotely while staying online     → caught by backup_engine
  3. Manual restore from dashboard (specific version)   → restore_router(config_id=X)
  4. Manual restore latest from dashboard               → restore_router()
  5. No backup exists yet                               → clear error, no crash
  6. Restore itself fails (SSH error)                   → alert sent, error logged
  7. Router has corrupted / partial config              → is_factory_default catches it
"""

import os
import logging
from ssh_manager import SSHManager
from database import (
    get_router, get_latest_config, get_config_by_id,
    update_router_status, log_event, store_config
)

logger = logging.getLogger("rbrcs.restore")
ssh = SSHManager()


# ── Factory Default Detection ──────────────────────────────

# Minimum meaningful config lines per device type.
# If the fetched config has fewer lines than this, it is treated as factory default.
FACTORY_LINE_THRESHOLD = {
    "cisco_ios":          20,
    "mikrotik_routeros":  10,
    "ubiquiti_edgeos":    10,
    "generic_linux":       5,
}

# Default hostname strings that indicate a fresh/reset device
FACTORY_HOSTNAMES = {
    "cisco_ios": {"hostname Router", "hostname Switch", "hostname router", "hostname switch"},
    "mikrotik_routeros": set(),
    "ubiquiti_edgeos": set(),
    "generic_linux": set(),
}

# Keywords that MUST be present in a real (non-factory) config
# If none of these exist, config is likely factory default
REQUIRED_KEYWORDS = {
    "cisco_ios": ["interface", "ip address", "username"],
    "mikrotik_routeros": ["/ip", "/interface"],
    "ubiquiti_edgeos": ["interfaces", "address"],
    "generic_linux": ["address", "gateway", "interface"],
}


def is_factory_default(config_text, device_type):
    """
    Heuristic check: is this config a factory/blank default?

    Uses THREE independent signals — ALL must agree to avoid false positives:
      1. Config is empty or near-empty (< 10 chars)
      2. Total non-blank line count is below threshold (includes comments)
      3. Default hostname detected  OR  none of the required keywords present

    Using total lines (including comments) avoids false positives on configs
    that have many ! comment lines but few actual config lines.

    Returns True only if the config clearly looks like a factory default.
    """
    if not config_text or len(config_text.strip()) < 10:
        logger.debug("Factory default: config is empty or near-empty")
        return True

    # Count ALL non-blank lines (including comments) — real configs are long
    all_lines = [l.strip() for l in config_text.split("\n") if l.strip()]
    threshold = FACTORY_LINE_THRESHOLD.get(device_type, 20)
    if len(all_lines) < threshold:
        logger.debug(f"Factory default: only {len(all_lines)} total lines (threshold: {threshold})")
        return True

    # Strip comments for keyword/hostname checks
    config_lines = [
        l.strip() for l in config_text.split("\n")
        if l.strip() and not l.strip().startswith("!")
        and not l.strip().startswith("#")
    ]

    # Check for default factory hostname
    factory_hostnames = FACTORY_HOSTNAMES.get(device_type, set())
    for line in config_lines:
        if line in factory_hostnames:
            logger.debug(f"Factory default: found factory hostname: '{line}'")
            return True

    # Check that at least one required keyword exists in the config
    required = REQUIRED_KEYWORDS.get(device_type, [])
    if required:
        config_lower = config_text.lower()
        has_required = any(kw.lower() in config_lower for kw in required)
        if not has_required:
            logger.debug(f"Factory default: none of required keywords found: {required}")
            return True

    return False


# ── Restore Logic ──────────────────────────────────────────

def restore_router(router_id, config_id=None, db_path=None):
    """
    Restore a router's configuration.

    Args:
        router_id : ID of the router to restore
        config_id : Specific backup version to restore (None = use latest non-factory backup)

    Returns:
        dict with keys: success, message
    """
    router = get_router(router_id, db_path)
    if not router:
        return {"success": False, "message": "Router not found in database"}

    # ── Find the config to restore ─────────────────────────
    if config_id:
        # Specific version requested (from dashboard)
        config = get_config_by_id(config_id, db_path)
        if not config:
            return {"success": False, "message": f"Config ID {config_id} not found"}
        if config["router_id"] != router_id:
            return {"success": False, "message": "Config does not belong to this router"}
    else:
        # Auto-restore: find latest backup that is NOT a factory default
        config = _get_last_good_config(router_id, router.get("device_type"), db_path)
        if not config:
            msg = ("No valid backup found for this router. "
                   "At least one successful backup must exist before auto-restore can work.")
            logger.error(f"{router['name']}: {msg}")
            log_event(router_id, "restore_error", msg, "error", db_path)
            return {"success": False, "message": msg}

    logger.info(f"Restoring {router['name']} from config ID {config['id']} "
                f"(timestamp: {config.get('timestamp', 'unknown')})")

    try:
        # ── Push config via SSH ────────────────────────────
        success, message = ssh.push_config(router, config["config_text"])

        if success:
            full_msg = f"Restored config ID {config['id']} ({config.get('timestamp', '')}): {message}"
            logger.info(f"{router['name']}: {full_msg}")
            log_event(router_id, "restore_success", full_msg, "info", db_path)

            # Save restored config as a new backup entry so history is complete
            store_config(router_id, config["config_text"], "restore", db_path)

            # Send success alert
            _send_alert(router, "restore_success",
                        f"✅ Auto-restore SUCCESS on {router['name']} ({router['host']}). "
                        f"Config ID {config['id']} restored.")
        else:
            logger.error(f"{router['name']}: Restore push FAILED — {message}")
            log_event(router_id, "restore_error", message, "error", db_path)

            # Send failure alert — this requires human intervention
            _send_alert(router, "restore_failed",
                        f"❌ RESTORE FAILED on {router['name']} ({router['host']}). "
                        f"Manual intervention required! Error: {message}")

        return {"success": success, "message": message}

    except Exception as e:
        msg = f"Restore exception: {str(e)}"
        logger.error(f"{router['name']}: {msg}")
        log_event(router_id, "restore_error", msg, "error", db_path)
        _send_alert(router, "restore_failed",
                    f"❌ RESTORE EXCEPTION on {router['name']}: {msg}")
        return {"success": False, "message": msg}


def _get_last_good_config(router_id, device_type, db_path=None):
    """
    Find the most recent backup that is NOT a factory default config.
    Scans up to the last 10 backups.

    This protects against the edge case where a factory config was
    accidentally saved before the reset was detected.
    """
    from database import get_config_history, get_config_by_id
    history = get_config_history(router_id, limit=10, db_path=db_path)

    for entry in history:
        # Skip entries tagged as factory/restore if needed
        if entry.get("change_type") == "factory_reset":
            continue

        config = get_config_by_id(entry["id"], db_path)
        if config and not is_factory_default(config["config_text"], device_type):
            logger.debug(f"Found good config: ID {entry['id']} (type: {entry.get('change_type')})")
            return config

    logger.warning(f"No non-factory config found in last 10 backups for router {router_id}")
    return None


# ── Auto-Restore Check (called by health_checker on reconnect) ──────────

def check_and_auto_restore(router_id, db_path=None):
    """
    Called when a router comes back online after being offline.
    Checks if it has a factory default config and restores if so.

    Returns:
        dict with keys: was_reset (bool), restore_result (dict|None)
    """
    router = get_router(router_id, db_path)
    if not router:
        return {"was_reset": False, "restore_result": None}

    try:
        # Fetch current live config from router
        current_config = ssh.fetch_config(router)

        if is_factory_default(current_config, router["device_type"]):
            logger.warning(f"{router['name']}: Factory default config detected after reconnect — auto-restoring")
            log_event(router_id, "factory_reset_detected",
                      "Router has factory default config after coming back online. Auto-restore initiated.",
                      "warning", db_path)

            _send_alert(router, "factory_reset_detected",
                        f"🚨 Factory reset detected on {router['name']} ({router['host']}) "
                        f"after reconnect. Auto-restore initiated.")

            result = restore_router(router_id, db_path=db_path)
            return {"was_reset": True, "restore_result": result}

        # Config looks normal — no restore needed
        return {"was_reset": False, "restore_result": None}

    except Exception as e:
        logger.error(f"Auto-restore check failed for {router['name']}: {e}")
        return {"was_reset": False, "restore_result": None}


# ── Alert Helper ───────────────────────────────────────────

def _send_alert(router, event_type, message):
    """Send a webhook/Telegram alert if configured in config.yaml."""
    try:
        import yaml, requests
        with open("config.yaml", "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(os.path.expandvars(f.read()))
        webhook_url = cfg.get("alerts", {}).get("webhook_url", "")
        if webhook_url:
            requests.post(webhook_url, json={"text": message}, timeout=5)
            logger.debug(f"Alert sent for event: {event_type}")
    except Exception as e:
        logger.debug(f"Alert not sent ({event_type}): {e}")
