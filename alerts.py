"""
alerts.py — Centralized webhook alert dispatch.

Single module for all alert sending. Caches config.yaml in memory
so we don't re-read the file on every alert call.
"""

import os
import logging

logger = logging.getLogger("rbrcs.alerts")

_cached_webhook_url = None
_config_loaded = False


def _load_webhook_url():
    """Load webhook URL from config.yaml (cached after first call)."""
    global _cached_webhook_url, _config_loaded
    if _config_loaded:
        return _cached_webhook_url
    try:
        import yaml
        with open("config.yaml", "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(os.path.expandvars(f.read()))
        _cached_webhook_url = cfg.get("alerts", {}).get("webhook_url", "")
    except Exception:
        _cached_webhook_url = ""
    _config_loaded = True
    return _cached_webhook_url


def send_alert(router, event_type, message):
    """Send webhook alert if configured. Safe to call from anywhere."""
    url = _load_webhook_url()
    if not url:
        return
    try:
        import requests
        requests.post(url, json={"text": message}, timeout=5)
    except Exception:
        pass


def reload_config():
    """Force reload of webhook URL (call after config.yaml changes)."""
    global _config_loaded
    _config_loaded = False
