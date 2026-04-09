"""
restore_engine.py — Configuration restore logic (Production-Grade).

RESTORE PRIORITY ORDER:
  1. Specific config_id (if requested from dashboard)
  2. Golden config (if set for this router)
  3. Last known good backup (scans last 10, skips factory configs)

SOLVES:
  #1  Factory Reset          → auto-restore with smart config selection
  #5  Config Overwrite       → golden config always available as restore source
  #7  Firmware Upgrade Reset → same factory detection + restore
  #12 No Backup Available    → clear error message, no crash
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

FACTORY_LINE_THRESHOLD = {
    "cisco_ios": 20, "mikrotik_routeros": 10,
    "ubiquiti_edgeos": 10, "generic_linux": 5,
}

FACTORY_HOSTNAMES = {
    "cisco_ios": {"hostname Router", "hostname Switch", "hostname router", "hostname switch"},
    "mikrotik_routeros": set(), "ubiquiti_edgeos": set(), "generic_linux": set(),
}

REQUIRED_KEYWORDS = {
    "cisco_ios": ["interface", "ip address", "username"],
    "mikrotik_routeros": ["/ip", "/interface"],
    "ubiquiti_edgeos": ["interfaces", "address"],
    "generic_linux": ["address", "gateway", "interface"],
}


def is_factory_default(config_text, device_type):
    """
    3-signal heuristic: empty → line count → hostname/keyword check.
    Returns True only if config clearly looks like factory default.
    """
    if not config_text or len(config_text.strip()) < 10:
        return True

    all_lines = [l.strip() for l in config_text.split("\n") if l.strip()]
    threshold = FACTORY_LINE_THRESHOLD.get(device_type, 20)
    if len(all_lines) < threshold:
        return True

    config_lines = [l.strip() for l in config_text.split("\n")
                    if l.strip() and not l.strip().startswith("!")
                    and not l.strip().startswith("#")]

    for line in config_lines:
        if line in FACTORY_HOSTNAMES.get(device_type, set()):
            return True

    required = REQUIRED_KEYWORDS.get(device_type, [])
    if required:
        config_lower = config_text.lower()
        if not any(kw.lower() in config_lower for kw in required):
            return True

    return False


# ── Restore Logic ──────────────────────────────────────────

def restore_router(router_id, config_id=None, db_path=None):
    """
    Restore a router's config using priority: specific_id → golden → last_good_backup.
    """
    router = get_router(router_id, db_path)
    if not router:
        return {"success": False, "message": "Router not found"}

    # ── Find config to restore ────────────────────────────
    if config_id:
        config = get_config_by_id(config_id, db_path)
        if not config:
            return {"success": False, "message": f"Config ID {config_id} not found"}
        if config["router_id"] != router_id:
            return {"success": False, "message": "Config does not belong to this router"}
        source = f"specific backup #{config_id}"
    else:
        # Priority 1: Try golden config
        from golden_config import get_golden_config
        golden = get_golden_config(router_id, db_path)
        if golden:
            config = golden
            source = "golden config"
        else:
            # Priority 2: Last known good backup
            config = _get_last_good_config(router_id, router.get("device_type"), db_path)
            if not config:
                msg = ("No valid backup or golden config found. "
                       "At least one backup must exist before auto-restore can work.")
                logger.error(f"{router['name']}: {msg}")
                log_event(router_id, "restore_error", msg, "error", db_path)
                return {"success": False, "message": msg}
            source = f"last good backup #{config['id']}"

    logger.info(f"Restoring {router['name']} from {source}")

    try:
        success, message = ssh.push_config(router, config["config_text"])

        if success:
            full_msg = f"Restored from {source}: {message}"
            logger.info(f"{router['name']}: {full_msg}")
            log_event(router_id, "restore_success", full_msg, "info", db_path)
            store_config(router_id, config["config_text"], "restore", db_path)
            _send_alert(router, "restore_success",
                        f"✅ Restore SUCCESS on {router['name']} from {source}")
        else:
            logger.error(f"{router['name']}: Restore FAILED — {message}")
            log_event(router_id, "restore_error", message, "error", db_path)
            _send_alert(router, "restore_failed",
                        f"❌ Restore FAILED on {router['name']}: {message}")

        return {"success": success, "message": message}

    except Exception as e:
        msg = f"Restore exception: {str(e)}"
        logger.error(f"{router['name']}: {msg}")
        log_event(router_id, "restore_error", msg, "error", db_path)
        _send_alert(router, "restore_failed", f"❌ {router['name']}: {msg}")
        return {"success": False, "message": msg}


def _get_last_good_config(router_id, device_type, db_path=None):
    """Find the most recent backup that is NOT a factory default. Scans last 10."""
    from database import get_config_history, get_config_by_id
    history = get_config_history(router_id, limit=10, db_path=db_path)

    for entry in history:
        if entry.get("change_type") == "factory_reset":
            continue
        config = get_config_by_id(entry["id"], db_path)
        if config and not is_factory_default(config["config_text"], device_type):
            return config

    return None


# ── Auto-Restore Check ────────────────────────────────────

def check_and_auto_restore(router_id, db_path=None):
    """Called when router comes back online. Checks for factory config."""
    router = get_router(router_id, db_path)
    if not router:
        return {"was_reset": False, "restore_result": None}

    try:
        current_config = ssh.fetch_config(router)

        if is_factory_default(current_config, router["device_type"]):
            logger.warning(f"{router['name']}: Factory config detected — auto-restoring")
            log_event(router_id, "factory_reset_detected",
                      "Factory default config after reconnect. Auto-restore initiated.",
                      "warning", db_path)
            _send_alert(router, "factory_reset_detected",
                        f"🚨 Factory reset on {router['name']} ({router['host']}). Restoring...")

            result = restore_router(router_id, db_path=db_path)
            return {"was_reset": True, "restore_result": result}

        return {"was_reset": False, "restore_result": None}

    except Exception as e:
        logger.error(f"Auto-restore check failed for {router['name']}: {e}")
        return {"was_reset": False, "restore_result": None}


# ── Alert Helper ───────────────────────────────────────────

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
