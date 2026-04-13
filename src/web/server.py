"""
server.py — Concise Flask API for RBRCS.
"""
import os, sys, time, logging, yaml, difflib, csv, openpyxl, signal
from flask import Flask, jsonify, request, send_from_directory, session
from io import StringIO
from concurrent.futures import ThreadPoolExecutor

from src.core.database import *
from src.core.health import check_all_routers
from src.core.backup import backup_router, backup_all
from src.core.restore import restore_router
from src.core.retention import get_retention_stats
from src.core.compliance import generate_security_report
from src.utils.ssh import SSHManager
from src.utils.syslog import SyslogListener
from src.scheduler import start_scheduler, stop_scheduler, get_scheduled_jobs

logger = logging.getLogger("rbrcs.web")
STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
app = Flask(__name__, static_folder=STATIC_DIR)

# ── Load Config ────────────────────────────────────────────
CONFIG_PATH = "config.yaml"
def load_config():
    if not os.path.exists(CONFIG_PATH): return {}
    with open(CONFIG_PATH, "r") as f: return yaml.safe_load(os.path.expandvars(f.read()))

config = load_config()
app.secret_key = config.get("system", {}).get("secret_key", "default")

@app.route("/") def serve_index(): return send_from_directory(STATIC_DIR, "index.html")
@app.route("/api/stats") def api_stats(): return jsonify(get_dashboard_stats())
@app.route("/api/routers", methods=["GET", "POST"])
def api_routers():
    if request.method == "POST":
        upsert_router(request.json); return jsonify({"success": True})
    return jsonify(get_all_routers())

@app.route("/api/routers/test", methods=["POST"])
def api_test_router():
    ok, msg = SSHManager().test_connection(request.json)
    return jsonify({"success": ok, "message": msg})

@app.route("/api/routers/<rid>", methods=["GET", "DELETE"])
def api_router_detail(rid):
    if request.method == "DELETE": delete_router(rid); return jsonify({"success": True})
    return jsonify(get_router(rid))

@app.route("/api/routers/<rid>/history") def api_history(rid): return jsonify(get_config_history(rid))
@app.route("/api/backup/<rid>") def api_bkp(rid): return jsonify(backup_router(rid, "manual"))
@app.route("/api/restore/<rid>") def api_res(rid): return jsonify(restore_router(rid, request.args.get("config_id", type=int)))
@app.route("/api/jobs") def api_jobs(): return jsonify(get_scheduled_jobs())
@app.route("/api/retention-stats") def api_ret(): return jsonify(get_retention_stats())

@app.route("/api/routers/<rid>/push", methods=["POST"])
def api_push(rid):
    r = get_router(rid)
    ok, out = SSHManager().execute_commands(r, request.json.get("commands", ""))
    return jsonify({"success": ok, "output": out})

@app.route("/api/routers/mass-push", methods=["POST"])
def api_mass_push():
    data = request.json; ids = data.get("router_ids", []); cmds = data.get("commands", "")
    def worker(rid):
        r = get_router(rid)
        if not r: return rid, {"success": False, "out": "Not found"}
        c = cmds
        for k, v in r.items(): c = c.replace(f"{{{{{k}}}}}", str(v))
        ok, out = SSHManager().execute_commands(r, c)
        return rid, {"success": ok, "output": out}
    with ThreadPoolExecutor(max_workers=10) as ex:
        res = dict(ex.map(worker, ids))
    return jsonify({"success": True, "results": res})

@app.route("/api/routers/<rid>/compliance")
def api_comp(rid):
    r = get_router(rid); cfg = get_config(rid=rid)
    return jsonify(generate_security_report(r["device_type"], cfg["config_text"] if cfg else ""))

def run():
    init_db()
    for r in config.get("routers", []): upsert_router(r)
    start_scheduler(config)
    
    syslog_cfg = config.get("syslog", {})
    if syslog_cfg.get("enabled"):
        SyslogListener(syslog_cfg.get("host", "0.0.0.0"), syslog_cfg.get("port", 514)).start()
        
    app.run(host="0.0.0.0", port=config.get("system", {}).get("web_port", 8080))
