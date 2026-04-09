"""
restore_engine.py — Configuration restore logic.

Handles:
  - Manual restore from a specific backup
  - Auto-restore when factory reset detected
"""

import logging
from ssh_manager import SSHManager
from database import (
    get_router, get_latest_config, get_config_by_id,
    update_router_status, log_event, compute_hash, store_config
)

logger = logging.getLogger("rbrcs.restore")
ssh = SSHManager()

# Known "factory default" config hashes per device type
# These are short/minimal configs that indicate a factory reset
DEFAULT_CONFIG_INDICATORS = {
    "cisco_ios": [
        "no service timestamps",
        "hostname Router",
        "line con 0",
    ],
    "mikrotik_routeros": [
        "# software id",
        "/ip address",
    ],
    "ubiquiti_edgeos": [
        "firewall {",
        "system {",
    ],
}


def is_factory_default(config_text, device_type):
    """
    Heuristic check: is this config a factory default?
    Detects by checking if the config is very short or matches default patterns.
    """
    if not config_text:
        return True

    lines = [l.strip() for l in config_text.split("\n") if l.strip() and not l.strip().startswith("!")]

    # Very short config (< 20 meaningful lines) likely means default
    if len(lines) < 20:
        return True

    # Check for default hostname
    if device_type == "cisco_ios":
        for line in lines:
            if line.startswith("hostname") and line.strip() in ("hostname Router", "hostname Switch"):
                return True

    return False


def restore_router(router_id, config_id=None, db_path=None):
    """
    Restore a router's configuration.

    Args:
        router_id: ID of the router to restore
        config_id: Specific config version to restore (None = latest)

    Returns:
        dict with keys: success, message
    """
    router = get_router(router_id, db_path)
    if not router:
        return {"success": False, "message": "Router not found"}

    # Get the config to restore
    if config_id:
        config = get_config_by_id(config_id, db_path)
        if not config:
            return {"success": False, "message": f"Config ID {config_id} not found"}
        if config["router_id"] != router_id:
            return {"success": False, "message": "Config does not belong to this router"}
    else:
        config = get_latest_config(router_id, db_path)
        if not config:
            return {"success": False, "message": "No backup found for this router"}

    logger.info(f"Restoring {router['name']} from config ID {config['id']}")

    try:
        # Push config via SSH
        success, message = ssh.push_config(router, config["config_text"])

        if success:
            log_event(router_id, "restore_success",
                      f"Restored config ID {config['id']}: {message}", "info", db_path)
            # Store the restored config as a new backup entry
            store_config(router_id, config["config_text"], "restore", db_path)
        else:
            log_event(router_id, "restore_error", message, "error", db_path)

        return {"success": success, "message": message}

    except Exception as e:
        msg = f"Restore failed: {str(e)}"
        logger.error(msg)
        log_event(router_id, "restore_error", msg, "error", db_path)
        return {"success": False, "message": msg}


def check_and_auto_restore(router_id, db_path=None):
    """
    Check if a router has been factory-reset and auto-restore if so.

    Returns:
        dict with keys: was_reset, restore_result
    """
    router = get_router(router_id, db_path)
    if not router:
        return {"was_reset": False, "restore_result": None}

    try:
        # Fetch current config
        current_config = ssh.fetch_config(router)

        if is_factory_default(current_config, router["device_type"]):
            logger.warning(f"{router['name']}: Factory default config detected! Auto-restoring...")
            log_event(router_id, "factory_reset_detected",
                      "Router appears to have factory default config", "warning", db_path)

            # Webhook Alert
            try:
                import yaml, requests
                with open("config.yaml", "r", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f)
                webhook_url = cfg.get("alerts", {}).get("webhook_url")
                if webhook_url:
                    requests.post(webhook_url, json={
                        "text": f"🚨 *CRITICAL:* Factory reset detected on router `{router['name']}` ({router['host']}). Auto-restore initiated!"
                    }, timeout=5)
            except Exception as e:
                logger.error(f"Failed to send webhook alert: {e}")

            result = restore_router(router_id, db_path=db_path)
            return {"was_reset": True, "restore_result": result}

        return {"was_reset": False, "restore_result": None}

    except Exception as e:
        logger.error(f"Auto-restore check failed for {router['name']}: {e}")
        return {"was_reset": False, "restore_result": None}
