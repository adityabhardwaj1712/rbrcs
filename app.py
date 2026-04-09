"""
app.py — RBRCS Backend: Flask API + Scheduler + Syslog.

Serves the tactical dashboard (static/) and exposes REST API endpoints
for real-time router management, backup/restore, config viewing, and system stats.
"""

import os
import sys
import time
import logging
import signal
import threading
import yaml
import difflib

from flask import Flask, jsonify, request, send_from_directory, session

from database import (
    init_db, upsert_router, log_event, SQLiteHandler,
    get_all_routers, get_router, get_dashboard_stats,
    get_events, get_config_history, get_config_by_id,
    get_latest_config, get_total_storage_bytes,
    delete_router
)
from scheduler import start_scheduler, stop_scheduler, get_scheduled_jobs
from syslog_listener import SyslogListener
from backup_engine import backup_router
from restore_engine import restore_router
from ssh_manager import SSHManager
from retention import get_retention_stats
from compliance import generate_security_report

# ── Logging ────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        SQLiteHandler()
    ]
)
logger = logging.getLogger("rbrcs.core")

# ── Load Config ────────────────────────────────────────────

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")

def load_config():
    """Load config.yaml with env var expansion."""
    if not os.path.exists(CONFIG_PATH):
        logger.error(f"Configuration file not found: {CONFIG_PATH}")
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        expanded = os.path.expandvars(f.read())
        return yaml.safe_load(expanded)

config = load_config()

# Set DB path from config
import database
database.DB_PATH = config.get("system", {}).get("db_path", "rbrcs.db")

# ── Flask App ──────────────────────────────────────────────

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app = Flask(__name__, static_folder=STATIC_DIR)
app.config["SECRET_KEY"] = config.get("system", {}).get("secret_key", "rbrcs-default")

# Disable Flask's verbose request logging for lighter output
log = logging.getLogger("werkzeug")
log.setLevel(logging.WARNING)


# ── Static / Dashboard ────────────────────────────────────

@app.route("/")
def serve_dashboard():
    return send_from_directory(STATIC_DIR, "index.html")

@app.route("/static/<path:filename>")
def serve_static(filename):
    return send_from_directory(STATIC_DIR, filename)


# ── Authentication ────────────────────────────────────────

@app.before_request
def require_auth():
    if request.path.startswith("/static/") or request.path == "/":
        return
    if request.path in ["/api/login", "/api/auth/status"]:
        return
    
    auth_cfg = config.get("auth", {})
    if not auth_cfg.get("enabled", False):
        return
        
    if request.path.startswith("/api/"):
        if not session.get("logged_in"):
            return jsonify({"error": "Unauthorized"}), 401

@app.route("/api/auth/status")
def api_auth_status():
    auth_cfg = config.get("auth", {})
    if not auth_cfg.get("enabled", False):
        return jsonify({"enabled": False, "logged_in": True})
    return jsonify({
        "enabled": True, 
        "logged_in": session.get("logged_in", False)
    })

@app.route("/api/login", methods=["POST"])
def api_login():
    auth_cfg = config.get("auth", {})
    payload = request.json or {}
    username = payload.get("username")
    password = payload.get("password")
    
    if username == auth_cfg.get("admin_username") and password == auth_cfg.get("admin_password"):
        session["logged_in"] = True
        return jsonify({"success": True})
    return jsonify({"error": "Invalid credentials"}), 401

@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"success": True})


# ── API: Dashboard Stats ──────────────────────────────────

def update_config_yaml_routers(routers_list):
    from ruamel.yaml import YAML
    yaml_parser = YAML()
    yaml_parser.preserve_quotes = True
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        data = yaml_parser.load(f)
    if not data:
        data = {}
    data["routers"] = routers_list
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml_parser.dump(data, f)
    # Sync runtime config variable and database
    global config
    config = load_config()

@app.route("/api/stats")
def api_stats():
    return jsonify(get_dashboard_stats())

@app.route("/api/routers", methods=["GET", "POST"])
def api_routers():
    if request.method == "POST":
        router_data = request.json
        upsert_router(router_data)
        routers = get_all_routers()
        update_config_yaml_routers(routers)
        return jsonify({"success": True})
    return jsonify(get_all_routers())

@app.route("/api/routers/<router_id>", methods=["GET", "DELETE"])
def api_router_detail(router_id):
    if request.method == "DELETE":
        delete_router(router_id)
        routers = get_all_routers()
        update_config_yaml_routers(routers)
        return jsonify({"success": True})
    r = get_router(router_id)
    if not r:
        return jsonify({"error": "Not found"}), 404
    return jsonify(r)

@app.route("/api/routers/<router_id>/history")
def api_router_history(router_id):
    limit = request.args.get("limit", 50, type=int)
    return jsonify(get_config_history(router_id, limit))

@app.route("/api/routers/<router_id>/config/<int:config_id>")
def api_config_detail(router_id, config_id):
    cfg = get_config_by_id(config_id)
    if not cfg:
        return jsonify({"error": "Config not found"}), 404
    if cfg["router_id"] != router_id:
        return jsonify({"error": "Config does not belong to this router"}), 403
    # Remove binary blob from response
    cfg.pop("config_data", None)
    return jsonify(cfg)


# ── API: Events ───────────────────────────────────────────

@app.route("/api/events")
def api_events():
    limit = request.args.get("limit", 100, type=int)
    router_id = request.args.get("router_id", None)
    return jsonify(get_events(limit, router_id))


# ── API: Scheduled Jobs ──────────────────────────────────

@app.route("/api/jobs")
def api_jobs():
    return jsonify(get_scheduled_jobs())


# ── API: Retention Stats ─────────────────────────────────

@app.route("/api/retention-stats")
def api_retention():
    return jsonify(get_retention_stats())


# ── API: Backup Trigger ──────────────────────────────────

@app.route("/api/backup/<router_id>")
def api_backup(router_id):
    result = backup_router(router_id, change_type="manual")
    return jsonify(result)


# ── API: Restore Trigger ─────────────────────────────────

@app.route("/api/restore/<router_id>")
def api_restore(router_id):
    config_id = request.args.get("config_id", None, type=int)
    result = restore_router(router_id, config_id=config_id)
    return jsonify(result)

# ── API: Push Config ─────────────────────────────────────

@app.route("/api/routers/<router_id>/push", methods=["POST"])
def api_push_config(router_id):
    router = get_router(router_id)
    if not router:
        return jsonify({"error": "Router not found"}), 404
    commands = request.json.get("commands", "")
    if not commands:
        return jsonify({"error": "No commands provided"}), 400
    ssh = SSHManager()
    success, output = ssh.execute_commands(router, commands)
    return jsonify({"success": success, "output": output})


# ── API: Compliance Trigger ────────────────────────────────

@app.route("/api/routers/<router_id>/compliance")
def api_get_compliance(router_id):
    router = get_router(router_id)
    if not router:
        return jsonify({"error": "Router not found"}), 404
        
    cfg = get_latest_config(router_id)
    config_text = cfg["config_text"] if cfg else ""
    
    report = generate_security_report(router["device_type"], config_text)
    return jsonify(report)


# ── API: Config Diff ─────────────────────────────────────

@app.route("/api/config/diff")
def api_config_diff():
    router_id = request.args.get("router_id")
    config_id = request.args.get("config_id", type=int)
    if not router_id or not config_id:
        return jsonify({"error": "router_id and config_id required"}), 400

    current = get_config_by_id(config_id)
    if not current:
        return jsonify({"error": "Config not found"}), 404

    # Find previous config
    history = get_config_history(router_id, limit=50)
    prev = None
    found_current = False
    for h in history:
        if found_current:
            prev = get_config_by_id(h["id"])
            break
        if h["id"] == config_id:
            found_current = True

    if not prev:
        return jsonify({"error": "No previous config to compare against"})

    diff_lines = list(difflib.unified_diff(
        prev["config_text"].splitlines(),
        current["config_text"].splitlines(),
        fromfile=f"Backup #{prev['id']}",
        tofile=f"Backup #{current['id']}",
        lineterm=""
    ))

    additions = sum(1 for l in diff_lines if l.startswith('+') and not l.startswith('+++'))
    deletions = sum(1 for l in diff_lines if l.startswith('-') and not l.startswith('---'))

    return jsonify({
        "current_id": config_id,
        "previous_id": prev["id"],
        "diff": "\n".join(diff_lines),
        "additions": additions,
        "deletions": deletions,
    })


# ── Termination Handler ──────────────────────────────────

def graceful_shutdown(signum, frame):
    logger.info("Termination signal received. Shutting down...")
    stop_scheduler()
    sys.exit(0)

signal.signal(signal.SIGINT, graceful_shutdown)
signal.signal(signal.SIGTERM, graceful_shutdown)


# ── Main ───────────────────────────────────────────────────

def run():
    """Main execution: init DB, start scheduler, start Flask."""
    logger.info("Initializing RBRCS Core Service...")

    # Initialize DB
    init_db()

    # Sync routers from config.yaml into database
    routers = config.get("routers", [])
    for router_cfg in routers:
        upsert_router(router_cfg)
        logger.info(f"Synchronized node: {router_cfg['name']} ({router_cfg['host']})")

    log_event(None, "service_start", "RBRCS service initialized", "info")

    # Start scheduler
    start_scheduler(config)
    logger.info("Scheduler online.")

    # Start syslog listener if enabled
    syslog_cfg = config.get("syslog", {})
    if syslog_cfg.get("enabled", False):
        listen_host = syslog_cfg.get("listen_host", "0.0.0.0")
        listen_port = syslog_cfg.get("listen_port", 514)
        syslog = SyslogListener(host=listen_host, port=listen_port)
        syslog.start()
        logger.info(f"Syslog listener on {listen_host}:{listen_port}")

    # Start Flask API server
    port = config.get("system", {}).get("web_port", 8080)
    logger.info(f"Dashboard live at http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    run()
