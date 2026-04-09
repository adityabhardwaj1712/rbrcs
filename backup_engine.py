"""
backup_engine.py — Core backup logic.

Fetches router config via SSH, hashes it, and stores only if changed.
"""

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
        # Fetch config via SSH
        config_text = ssh.fetch_config(router)

        if not config_text or len(config_text.strip()) < 10:
            msg = "Empty or invalid config received"
            logger.warning(f"{router['name']}: {msg}")
            log_event(router_id, "backup_warning", msg, "warning", db_path)
            update_router_status(router_id, "online", db_path)
            return {"success": False, "router_id": router_id,
                    "message": msg, "is_new": False, "config_id": None}

        # Store config (with dedup)
        config_id, is_new = store_config(router_id, config_text, change_type, db_path)
        update_router_status(router_id, "online", db_path)

        if is_new:
            msg = f"New config saved (ID: {config_id}, size: {len(config_text)} bytes)"
            
            # Compute quick diff summary if there is a previous config
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
                        msg += f" (Changes: +{additions} / -{deletions} lines)"
            except Exception as e:
                logger.error(f"Failed to generate diff summary: {e}")

            logger.info(f"{router['name']}: {msg}")
            log_event(router_id, "backup_new", msg, "info", db_path)
            
            # Run compliance scanner against the new text
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
