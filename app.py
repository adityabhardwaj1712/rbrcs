"""
app.py — Flask web dashboard for RBRCS.

Main entry point. Starts the web server, scheduler, and syslog listener.
"""

import os
import sys
import difflib
import logging
import yaml
from flask import Flask, render_template, request, jsonify, redirect, url_for

from database import (
    init_db, upsert_router, get_all_routers, get_router,
    get_dashboard_stats, get_config_history, get_config_by_id,
    get_events, log_event, get_total_storage_bytes, DB_PATH
)
from backup_engine import backup_router, backup_all
from restore_engine import restore_router
from scheduler import start_scheduler, stop_scheduler, get_scheduled_jobs
from syslog_listener import SyslogListener
from retention import get_retention_stats

# ── Logging ────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("rbrcs.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger("rbrcs.app")

# ── Load Config ────────────────────────────────────────────

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")


def load_config():
    """Load config.yaml with env var expansion."""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        expanded = os.path.expandvars(f.read())
        return yaml.safe_load(expanded)


config = load_config()

# ── Flask App ──────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = config.get("system", {}).get("secret_key", "default-secret")

# Set DB path from config
import database
database.DB_PATH = config.get("system", {}).get("db_path", "rbrcs.db")

# ── Initialize ─────────────────────────────────────────────

init_db()

# Sync routers from config.yaml into database
for router_cfg in config.get("routers", []):
    upsert_router(router_cfg)
    logger.info(f"Registered router: {router_cfg['name']} ({router_cfg['host']})")

log_event(None, "system_start", "RBRCS system started", "info")


# ── Utility ────────────────────────────────────────────────

def format_bytes(b):
    """Format bytes into human-readable string."""
    if b < 1024:
        return f"{b} B"
    elif b < 1024 * 1024:
        return f"{b / 1024:.1f} KB"
    else:
        return f"{b / (1024 * 1024):.1f} MB"


@app.template_filter("format_bytes")
def format_bytes_filter(b):
    return format_bytes(b)


@app.template_filter("time_ago")
def time_ago_filter(timestamp_str):
    """Convert a timestamp to 'X minutes ago' format."""
    if not timestamp_str:
        return "Never"
    from datetime import datetime
    try:
        ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        now = datetime.utcnow()
        diff = now - ts.replace(tzinfo=None)
        seconds = int(diff.total_seconds())
        if seconds < 60:
            return "just now"
        elif seconds < 3600:
            m = seconds // 60
            return f"{m}m ago"
        elif seconds < 86400:
            h = seconds // 3600
            return f"{h}h ago"
        else:
            d = seconds // 86400
            return f"{d}d ago"
    except Exception:
        return timestamp_str


# ── Routes ─────────────────────────────────────────────────

@app.context_processor
def inject_sidebar_routers():
    return dict(sidebar_routers=get_all_routers())

@app.route("/")
def dashboard():
    """Main dashboard."""
    stats = get_dashboard_stats()
    retention = get_retention_stats()
    jobs = get_scheduled_jobs()
    return render_template("dashboard.html",
                           stats=stats, retention=retention, jobs=jobs)


@app.route("/router/add")
def add_router_page():
    """Add Router UI."""
    return render_template("add_router.html")


@app.route("/router/<router_id>")
def router_detail(router_id):
    """Per-router detail page with backup history."""
    router = get_router(router_id)
    if not router:
        return "Router not found", 404

    history = get_config_history(router_id, limit=50)
    events = get_events(limit=30, router_id=router_id)
    return render_template("router_detail.html",
                           router=router, history=history, events=events)


@app.route("/diff/<int:config_id_a>/<int:config_id_b>")
def diff_configs(config_id_a, config_id_b):
    """Side-by-side config diff."""
    config_a = get_config_by_id(config_id_a)
    config_b = get_config_by_id(config_id_b)

    if not config_a or not config_b:
        return "Config not found", 404

    # Generate unified diff
    diff_lines = list(difflib.unified_diff(
        config_a["config_text"].splitlines(keepends=True),
        config_b["config_text"].splitlines(keepends=True),
        fromfile=f"Config #{config_id_a} ({config_a['timestamp']})",
        tofile=f"Config #{config_id_b} ({config_b['timestamp']})",
        lineterm=""
    ))

    return render_template("diff.html",
                           config_a=config_a, config_b=config_b,
                           diff_lines=diff_lines)


@app.route("/config/<int:config_id>")
def view_config(config_id):
    """View a specific config's full text."""
    config_data = get_config_by_id(config_id)
    if not config_data:
        return "Config not found", 404
    router = get_router(config_data["router_id"])
    return render_template("view_config.html", config=config_data, router=router)


@app.route("/events")
def events_page():
    """Event log page."""
    events = get_events(limit=200)
    return render_template("events.html", events=events)


@app.route("/settings")
def settings_page():
    """Settings page."""
    return render_template("settings.html", config=config)


# ── API Endpoints ──────────────────────────────────────────

@app.route("/api/backup/<router_id>", methods=["POST"])
def api_backup(router_id):
    """Trigger manual backup via API."""
    result = backup_router(router_id, change_type="manual")
    return jsonify(result)


@app.route("/api/backup-all", methods=["POST"])
def api_backup_all():
    """Trigger backup of all routers."""
    results = backup_all()
    return jsonify({"results": results})


@app.route("/api/restore/<router_id>", methods=["POST"])
def api_restore(router_id):
    """Restore a router config via API."""
    config_id = request.json.get("config_id") if request.is_json else None
    result = restore_router(router_id, config_id=config_id)
    return jsonify(result)


@app.route("/api/routers", methods=["GET", "POST"])
def api_routers():
    """Get all routers or add a new router."""
    if request.method == "POST":
        router_data = request.json
        # Format the data
        router_cfg = {
            "id": router_data.get("id"),
            "name": router_data.get("name"),
            "host": router_data.get("host"),
            "port": int(router_data.get("port", 22)),
            "device_type": router_data.get("device_type", "cisco_ios"),
            "username": router_data.get("username", ""),
            "password": router_data.get("password", ""),
            "enable_password": router_data.get("enable_password", "")
        }
        
        # Upsert in database
        upsert_router(router_cfg)

        # Append to config.yaml 
        import textwrap
        yaml_snippet = textwrap.dedent(f"""\n
          - id: "{router_cfg['id']}"
            name: "{router_cfg['name']}"
            host: "{router_cfg['host']}"
            port: {router_cfg['port']}
            device_type: "{router_cfg['device_type']}"
            username: "{router_cfg['username']}"
            password: "{router_cfg['password']}"
            enable_password: "{router_cfg['enable_password']}"
        """).lstrip()
        
        with open(CONFIG_PATH, "a", encoding="utf-8") as f:
            f.write(yaml_snippet)

        log_event(router_cfg['id'], "router_added", f"Successfully added router from UI", "info")
        return jsonify({"success": True, "message": "Router added successfully"})

    routers = get_all_routers()
    return jsonify(routers)


@app.route("/api/stats", methods=["GET"])
def api_stats():
    """Get dashboard stats."""
    stats = get_dashboard_stats()
    stats["total_storage_formatted"] = format_bytes(stats["total_storage"])
    return jsonify(stats)


@app.route("/api/config/<int:config_id>/download")
def api_download_config(config_id):
    """Download a config as a text file."""
    from flask import Response
    config_data = get_config_by_id(config_id)
    if not config_data:
        return "Not found", 404
    router = get_router(config_data["router_id"])
    filename = f"{router['name']}_{config_data['timestamp']}.txt".replace(" ", "_")
    return Response(
        config_data["config_text"],
        mimetype="text/plain",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@app.route("/api/router/<router_id>/configure", methods=["POST"])
def api_configure_router(router_id):
    """Execute raw configuration commands natively from the UI payload."""
    data = request.json
    commands = data.get("commands", "")
    
    router = get_router(router_id)
    if not router:
        return jsonify({"success": False, "message": "Router not found"}), 404

    if not commands.strip():
        return jsonify({"success": False, "message": "No commands provided"}), 400

    from ssh_manager import SSHManager
    mgr = SSHManager()
    success, output = mgr.execute_commands(router, commands)
    
    if success:
        log_event(router_id, "config_deployed", "Ad-hoc configuration pushed via Dashboard.", "warning")
        
    return jsonify({"success": success, "output": output})

@app.route("/api/health")
def api_health():
    """Simple healthcheck endpoint."""
    return jsonify({"status": "ok", "uptime": "running"})


@app.route("/api/export-all")
def api_export_all():
    """Export all latest configs as a ZIP file."""
    import zipfile
    import io
    from flask import send_file
    
    memory_file = io.BytesIO()
    routers = get_all_routers()
    
    with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
        for router in routers:
            from database import get_latest_config
            config_data = get_latest_config(router["id"])
            if config_data:
                filename = f"{router['name']}_{config_data['timestamp'].replace(':', '-')}.txt".replace(" ", "_")
                # Fix timestamp string to avoid invalid characters in filename
                zf.writestr(filename, config_data["config_text"])
                
    memory_file.seek(0)
    return send_file(
        memory_file,
        mimetype="application/zip",
        as_attachment=True,
        download_name="rbrcs_configs_export.zip"
    )

@app.route('/favicon.ico')
def favicon():
    """Serve a simple empty favicon to prevent 404s."""
    from flask import Response
    svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"></path></svg>'
    return Response(svg, mimetype="image/svg+xml")

# ── Main ───────────────────────────────────────────────────

if __name__ == "__main__":
    # Start scheduler
    start_scheduler(config)

    # Start syslog listener if enabled
    syslog_cfg = config.get("syslog", {})
    if syslog_cfg.get("enabled", False):
        syslog = SyslogListener(
            host=syslog_cfg.get("listen_host", "0.0.0.0"),
            port=syslog_cfg.get("listen_port", 514),
        )
        syslog.start()

    # Start Flask / Waitress
    dashboard_cfg = config.get("dashboard", {})
    host = dashboard_cfg.get("host", "0.0.0.0")
    port = dashboard_cfg.get("port", 5000)
    debug_mode = dashboard_cfg.get("debug", True)
    
    if debug_mode:
        logger.warning("Running Flask Development Server (debug: true)")
        app.run(
            host=host,
            port=port,
            debug=True,
            use_reloader=False,  # Prevent double-start with scheduler
        )
    else:
        logger.info(f"Starting Waitress Production Server on {host}:{port}")
        from waitress import serve
        serve(app, host=host, port=port, threads=2)
