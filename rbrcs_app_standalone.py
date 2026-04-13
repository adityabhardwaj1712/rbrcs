"""
rbrcs_app_standalone.py — Restructured & Compressed
"""
import csv
import difflib
import hashlib
import io
import logging
import os
import re
import signal
import socket
import socketserver
import sqlite3
import sys
import threading
import time
import yaml
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta
from io import StringIO

import openpyxl
import pandas as pd
import paramiko
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, Response, jsonify, request, send_from_directory, send_file, session
from waitress import serve
class MemoryLogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.buffer = []
        self.lock = threading.Lock()
    def emit(self, record):
        with self.lock:
            self.buffer.append(self.format(record))
            if len(self.buffer) > 200: self.buffer.pop(0)
    def get_logs(self):
        with self.lock: return list(self.buffer)

memory_handler = MemoryLogHandler()
memory_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), memory_handler]
)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

# ========================================
# MODULE: src/utils/alerts.py
# ========================================

def send_alert(router, etype, msg):
    try:
        with open("config.yaml", "r") as f: url = yaml.safe_load(os.path.expandvars(f.read())).get("alerts", {}).get("webhook_url")
        if url: requests.post(url, json={"text": msg}, timeout=5)
    except: pass


# ========================================
# MODULE: src/core/database.py
# ========================================
"""
database.py — Streamlined SQLite backend for RBRCS.
"""

DB_PATH = "data/rbrcs.db"

@contextmanager
def db_conn(path=None):
    conn = sqlite3.connect(path or DB_PATH)
    conn.row_factory = sqlite3.Row
    for pragma in ["journal_mode=WAL", "foreign_keys=ON", "auto_vacuum=FULL", "cache_size=-2000", "temp_store=MEMORY"]:
        conn.execute(f"PRAGMA {pragma}")
    try: yield conn; conn.commit()
    finally: conn.close()

def init_db():
    with db_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS routers (id TEXT PRIMARY KEY, name TEXT, host TEXT, port INTEGER DEFAULT 22, device_type TEXT, username TEXT, password TEXT, ssh_key_path TEXT, enable_password TEXT, status TEXT DEFAULT 'unknown', last_seen DATETIME, last_backup DATETIME, created_at DATETIME DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS configs (id INTEGER PRIMARY KEY AUTOINCREMENT, router_id TEXT, config_hash TEXT, config_data BLOB, config_size INTEGER, change_type TEXT DEFAULT 'auto', timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(router_id) REFERENCES routers(id) ON DELETE CASCADE);
            CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, router_id TEXT, event_type TEXT, message TEXT, severity TEXT DEFAULT 'info', timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(router_id) REFERENCES routers(id) ON DELETE SET NULL);
            CREATE TABLE IF NOT EXISTS system_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, logger TEXT, level TEXT, message TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS golden (rid TEXT PRIMARY KEY, hash TEXT, data BLOB, size INTEGER, promoted_at DATETIME DEFAULT CURRENT_TIMESTAMP, promoted_by TEXT DEFAULT 'system', FOREIGN KEY(rid) REFERENCES routers(id) ON DELETE CASCADE);
            CREATE INDEX IF NOT EXISTS idx_configs_rt ON configs(router_id, timestamp DESC);
            CREATE INDEX IF NOT EXISTS idx_events_rt ON events(timestamp DESC);
        """)

def upsert_router(r):
    with db_conn() as conn:
        conn.execute("""INSERT INTO routers (id, name, host, port, device_type, username, password, ssh_key_path, enable_password) VALUES (:id, :name, :host, :port, :device_type, :username, :password, :ssh_key_path, :enable_password)
            ON CONFLICT(id) DO UPDATE SET name=excluded.name, host=excluded.host, port=excluded.port, device_type=excluded.device_type, username=excluded.username, password=excluded.password, ssh_key_path=excluded.ssh_key_path, enable_password=excluded.enable_password""", 
            {**r, "name": r.get("name", r["id"]), "port": r.get("port", 22)})

def get_all_routers():
    with db_conn() as conn: return [dict(r) for r in conn.execute("SELECT * FROM routers ORDER BY name")]

def get_router(rid):
    with db_conn() as conn:
        r = conn.execute("SELECT * FROM routers WHERE id = ?", (rid,)).fetchone()
        return dict(r) if r else None

def delete_router(rid):
    with db_conn() as conn: conn.execute("DELETE FROM routers WHERE id = ?", (rid,))

def update_router_status(rid, status):
    with db_conn() as conn: conn.execute("UPDATE routers SET status = ?, last_seen = CURRENT_TIMESTAMP WHERE id = ?", (status, rid))

def store_config(rid, text, ctype="auto"):
    h = hashlib.md5(text.encode()).hexdigest()
    with db_conn() as conn:
        last = conn.execute("SELECT config_hash FROM configs WHERE router_id = ? ORDER BY timestamp DESC LIMIT 1", (rid,)).fetchone()
        if last and last[0] == h: return None, False
        cur = conn.execute("INSERT INTO configs (router_id, config_hash, config_data, config_size, change_type) VALUES (?, ?, ?, ?, ?)", (rid, h, zlib.compress(text.encode(), 6), len(text), ctype))
        conn.execute("UPDATE routers SET last_backup = CURRENT_TIMESTAMP WHERE id = ?", (rid,))
        return cur.lastrowid, True

def get_config(cid=None, rid=None):
    with db_conn() as conn:
        row = conn.execute("SELECT * FROM configs WHERE " + ("id=?" if cid else "router_id=? ORDER BY timestamp DESC LIMIT 1"), (cid or rid,)).fetchone()
        if not row: return None
        res = dict(row); res["config_text"] = zlib.decompress(res["config_data"]).decode()
        return res

def get_config_history(rid, limit=50):
    with db_conn() as conn: return [dict(r) for r in conn.execute("SELECT id, config_hash, config_size, change_type, timestamp FROM configs WHERE router_id = ? ORDER BY timestamp DESC LIMIT ?", (rid, limit))]

def log_event(rid, etype, msg, sev="info"):
    with db_conn() as conn: conn.execute("INSERT INTO events (router_id, event_type, message, severity) VALUES (?, ?, ?, ?)", (rid, etype, msg, sev))

def get_events(limit=100, rid=None):
    with db_conn() as conn:
        q = "SELECT * FROM events " + ("WHERE router_id=? " if rid else "") + "ORDER BY timestamp DESC LIMIT ?"
        return [dict(r) for r in conn.execute(q, (rid, limit) if rid else (limit,))]

def get_dashboard_stats():
    with db_conn() as conn:
        res = {k: conn.execute(v).fetchone()[0] for k, v in {
            "total_routers": "SELECT COUNT(*) FROM routers",
            "online_routers": "SELECT COUNT(*) FROM routers WHERE status='online'",
            "total_backups": "SELECT COUNT(*) FROM configs",
            "total_storage": "SELECT COALESCE(SUM(LENGTH(config_data)), 0) FROM configs"
        }.items()}
        res["recent_events"] = [dict(r) for r in conn.execute("SELECT e.*, r.name as router_name FROM events e LEFT JOIN routers r ON e.router_id = r.id ORDER BY e.timestamp DESC LIMIT 20")]
        res["routers"] = [dict(r) for r in conn.execute("SELECT r.*, (SELECT COUNT(*) FROM configs c WHERE c.router_id = r.id) as backup_count FROM routers r ORDER BY r.name")]
        return res

class SQLiteHandler(logging.Handler):
    def emit(self, record):
        try:
            with db_conn() as conn: conn.execute("INSERT INTO system_logs (logger, level, message) VALUES (?, ?, ?)", (record.name, record.levelname, self.format(record)))
        except: pass


# ========================================
# MODULE: src/utils/ssh.py
# ========================================
"""
ssh.py — Concise SSH manager for RBRCS.
"""

logger = logging.getLogger("rbrcs.ssh")
CMDS = {
    "cisco_ios": {"get": ["terminal length 0", "show run"], "pre": ["conf t"], "post": ["end", "wr mem"]},
    "mikrotik_routeros": {"get": ["/export"], "pre": [], "post": []},
    "ubiquiti_edgeos": {"get": ["show configuration"], "pre": ["configure"], "post": ["commit", "save", "exit"]},
}

class SSHManager:
    _pool = {}

    def __init__(self, timeout=30): self.timeout = timeout

    def _connect(self, r):
        rid = r.get("id")
        if rid in self._pool:
            c, t = self._pool[rid]
            if time.time() - t < 300 and c.get_transport() and c.get_transport().is_active(): return c
            try: c.close()
            except: pass
        
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        args = {"hostname": r["host"], "port": r.get("port", 22), "username": r.get("username", "admin"), "timeout": self.timeout, "look_for_keys": False, "allow_agent": False}
        if r.get("ssh_key_path"): args["key_filename"] = r["ssh_key_path"]
        else: args["password"] = r.get("password", "")
        
        c.connect(**args)
        if rid: self._pool[rid] = (c, time.time())
        return c

    def test_connection(self, r):
        try: self._connect(r); return True, "Connected"
        except Exception as e: return False, str(e)

    def ping(self, r):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5); res = s.connect_ex((r["host"], r.get("port", 22))); s.close()
            return res == 0
        except: return False

    def fetch_config(self, r):
        dtype = r.get("device_type", "cisco_ios")
        cmds = CMDS.get(dtype, CMDS["cisco_ios"])["get"]
        c = self._connect(r)
        if dtype == "cisco_ios":
            sh = c.invoke_shell(); time.sleep(1); sh.recv(65535)
            if r.get("enable_password"):
                sh.send("enable\n"); time.sleep(1); sh.recv(65535); sh.send(r["enable_password"]+"\n"); time.sleep(1); sh.recv(65535)
            res = ""
            for cmd in cmds:
                sh.send(cmd+"\n"); time.sleep(2)
                while sh.recv_ready(): res += sh.recv(65535).decode(errors="replace")
            sh.close()
            return res
        return "\n".join([c.exec_command(cmd)[1].read().decode(errors="replace") for cmd in cmds])

    def execute_commands(self, r, text):
        dtype = r.get("device_type", "cisco_ios")
        c = self._connect(r)
        if dtype == "cisco_ios":
            sh = c.invoke_shell(); time.sleep(1); sh.recv(65535)
            for line in (["enable", r["enable_password"]] if r.get("enable_password") else []) + ["conf t"] + text.split("\n") + ["end"]:
                if line: sh.send(line.strip()+"\n"); time.sleep(0.5)
            res = ""
            while sh.recv_ready() or time.sleep(1) or sh.recv_ready(): res += sh.recv(65535).decode(errors="replace")
            sh.close(); return True, res
        res = []
        for line in text.split("\n"):
            if not line.strip(): continue
            _, out, err = c.exec_command(line.strip())
            res.append(out.read().decode(errors="replace") + err.read().decode(errors="replace"))
        return True, "\n".join(res)

    def push_config(self, r, text):
        dtype = r.get("device_type", "cisco_ios")
        if dtype == "mikrotik_routeros":
            c = self._connect(r); sftp = c.open_sftp(); f = sftp.open("/restore.rsc", "w"); f.write(text); f.close(); sftp.close()
            _, out, err = c.exec_command("/import file=restore.rsc"); return not err.read(), "Imported"
        return self.execute_commands(r, text)


# ========================================
# MODULE: src/core/golden.py
# ========================================
"""
golden.py — Concise golden config (baseline) management for RBRCS.
"""

logger = logging.getLogger("rbrcs.golden")
CRITICAL = {"cisco_ios": ["hostname", "interface", "ip route"], "mikrotik_routeros": ["/ip address", "/ip route"]}

def set_golden_config(rid, text, user="system"):
    with db_conn() as conn:
        conn.execute("INSERT OR REPLACE INTO golden (rid, hash, data, size, promoted_by) VALUES (?, ?, ?, ?, ?)", (rid, hashlib.md5(text.encode()).hexdigest(), zlib.compress(text.encode(), 6), len(text), user))
    log_event(rid, "golden_set", f"Golden config set by {user}")


def get_golden_config(rid):
    with db_conn() as conn:
        try: row = conn.execute("SELECT * FROM golden WHERE rid=?", (rid,)).fetchone()
        except: return None
        if not row: return None
        res = dict(row); res["config_text"] = zlib.decompress(res["data"]).decode(); return res

def check_drift(rid, text, dtype):
    g = get_golden_config(rid)
    if not g: return None
    g_text = g["config_text"]
    d = list(difflib.unified_diff(g_text.splitlines(), text.splitlines(), n=0))
    adds = sum(1 for l in d if l.startswith('+') and not l.startswith('+++'))
    dels = sum(1 for l in d if l.startswith('-') and not l.startswith('---'))
    missing = [s for s in CRITICAL.get(dtype, []) if s.lower() not in text.lower()]
    
    if adds or dels or missing:
        msg = f"+{adds}/-{dels} lines drifted" + (f" | MISSING: {', '.join(missing)}" if missing else "")
        log_event(rid, "config_drift", msg, "error" if missing else "warning")
        return {"has_drift": True, "summary": msg, "missing": missing}
    return {"has_drift": False}

def check_corruption(text, dtype):
    if not text: return True, "Empty"
    non_pr = sum(1 for c in text if not c.isprintable() and c not in '\n\r\t')
    if (non_pr / len(text)) > 0.05: return True, "Garbage detected"
    if dtype == "cisco_ios" and "end" not in text.splitlines()[-5:]: return True, "Truncated"
    return False, ""


# ========================================
# MODULE: src/core/compliance.py
# ========================================
"""
compliance.py — Concise security scanner for RBRCS.
"""

RULES = {
    "cisco_ios": [
        ("C1", "error", "Plaintext enable", lambda l: l.startswith("enable password")),
        ("C2", "warning", "Weak Type 7", lambda l: "password 7 " in l),
        ("C3", "error", "Telnet input", lambda l: "transport input telnet" in l),
        ("C4", "warning", "HTTP server", lambda l: "ip http server" in l),
    ],
    "mikrotik_routeros": [
        ("M1", "error", "Default admin", lambda l: "name=admin" in l and "password=" not in l),
    ]
}

def generate_security_report(dtype, text):
    if not text: return {"score": 0, "grade": "N/A", "failed": []}
    failed = []
    for rid, sev, desc, trig in RULES.get(dtype, []):
        if any(trig(l.strip()) for l in text.split("\n")):
            failed.append({"id": rid, "severity": sev, "description": desc})
    
    score = max(0, 100 - sum(20 if f["severity"] == "error" else 10 for f in failed))
    grade = "A" if score >= 90 else "B" if score >= 80 else "C" if score >= 70 else "D" if score >= 60 else "F"
    return {"score": score, "grade": grade, "failed": failed}

def run_compliance_check(rid, dtype, text):
    rep = generate_security_report(dtype, text)
    for f in rep["failed"]: log_event(rid, "compliance_violation", f"{f['id']}: {f['description']}", f["severity"])


# ========================================
# MODULE: src/core/backup.py
# ========================================
"""
backup.py — Concise core backup logic for RBRCS.
"""

logger = logging.getLogger("rbrcs.backup")
ssh = SSHManager()

def backup_router(rid, ctype="auto"):
    r = get_router(rid)
    if not r: return {"success": False, "message": "Router not found"}
    logger.info(f"Backing up: {r['name']} ({r['host']})")

    try:
        text = ssh.fetch_config(r)
        if not text or len(text.strip()) < 10: return {"success": False, "message": "Empty config"}

        # Corruption & Factory Check
        
        is_corrupt, reason = check_corruption(text, r.get("device_type"))
        if is_corrupt:
            log_event(rid, "config_corrupted", reason, "error")
            send_alert(r, "config_corrupted", f"⚠️ Corrupt on {r['name']}: {reason}")
            return {"success": False, "message": f"Corrupt: {reason}"}

        if is_factory_default(text, r.get("device_type")):
            log_event(rid, "factory_reset", "Triggering auto-restore", "warning")
            send_alert(r, "factory_reset", f"🚨 Factory reset on {r['name']}. Restoring.")
            return restore_router(rid)

        # Store & Diff
        cid, is_new = store_config(rid, text, ctype)
        update_router_status(rid, "online")
        
        msg = f"Saved (ID: {cid})" if is_new else "Unchanged"
        if is_new:
            hist = get_config_history(rid, limit=2)
            if len(hist) >= 2:
                prev = get_config(cid=hist[1]["id"])
                if prev:
                    d = list(difflib.unified_diff(prev["config_text"].splitlines(), text.splitlines(), n=0))
                    msg += f" | Δ +{sum(1 for l in d if l.startswith('+') and not l.startswith('+++'))}/-{sum(1 for l in d if l.startswith('-') and not l.startswith('---'))}"
            
            # Drift Check
            if get_golden_config(rid):
                drift = check_drift(rid, text, r.get("device_type"))
                if drift and drift["has_drift"]: send_alert(r, "config_drift", f"⚠️ {r['name']}: {drift['summary']}")
            else: set_golden_config(rid, text, "auto-first")

        return {"success": True, "message": msg, "is_new": is_new, "config_id": cid}

    except Exception as e:
        logger.error(f"Backup failed: {e}")
        update_router_status(rid, "error")
        log_event(rid, "backup_error", str(e), "error")
        return {"success": False, "message": str(e)}

def backup_all(): return [backup_router(r["id"]) for r in get_all_routers()]


# ========================================
# MODULE: src/core/restore.py
# ========================================
"""
restore.py — Concise core restore logic for RBRCS.
"""

logger = logging.getLogger("rbrcs.restore")
ssh = SSHManager()

HINTS = {
    "cisco_ios": {"len": 20, "kw": ["interface", "ip address", "username"], "host": ["Router", "Switch"]},
    "mikrotik_routeros": {"len": 10, "kw": ["/ip", "/interface"], "host": []},
}

def is_factory_default(text, dtype):
    if not text or len(text.strip()) < 10: return True
    h = HINTS.get(dtype, HINTS["cisco_ios"])
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if len(lines) < h["len"]: return True
    if any(any(f"hostname {x}" in l for x in h["host"]) for l in lines): return True
    if h["kw"] and not any(kw in text.lower() for kw in h["kw"]): return True
    return False

def restore_router(rid, cid=None):
    r = get_router(rid)
    if not r: return {"success": False, "message": "Router not found"}
    
    cfg = None; src = ""
    if cid: 
        cfg = get_config(cid=cid); src = f"backup #{cid}"
    else:
        cfg = get_golden_config(rid); src = "golden"
        if not cfg:
            hist = get_config_history(rid, limit=10)
            for h in hist:
                c = get_config(cid=h["id"])
                if c and not is_factory_default(c["config_text"], r.get("device_type")): 
                    cfg = c; src = f"last good #{c['id']}"; break
    
    if not cfg: return {"success": False, "message": "No valid config source found"}
    logger.info(f"Restoring {r['name']} from {src}")

    try:
        ok, msg = ssh.push_config(r, cfg["config_text"])
        if ok:
            log_event(rid, "restore_success", f"From {src}: {msg}")
            send_alert(r, "restore_success", f"✅ Restore SUCCESS on {r['name']}")
        else:
            log_event(rid, "restore_error", msg, "error")
            send_alert(r, "restore_failed", f"❌ Restore FAILED on {r['name']}: {msg}")
        return {"success": ok, "message": msg}
    except Exception as e:
        return {"success": False, "message": str(e)}

def check_and_restore(rid):
    r = get_router(rid)
    if not r: return
    try:
        if is_factory_default(ssh.fetch_config(r), r["device_type"]):
            logger.warning(f"{r['name']}: Factory reset detected!")
            return restore_router(rid)
    except: pass


# ========================================
# MODULE: src/core/retention.py
# ========================================
"""
retention.py — Concise retention logic for RBRCS.
"""

def run_retention_cleanup(sets=None):
    c = sets or {}
    s = {
        "latest": c.get("keep_latest", 10),
        "days": c.get("daily_keep_days", 30),
        "weeks": c.get("weekly_keep_weeks", 26),
        "events": c.get("event_retention_days", 30)
    }
    with db_conn() as conn:
        for rid in [r[0] for r in conn.execute("SELECT DISTINCT id FROM routers")]:
            cfgs = conn.execute("SELECT id, timestamp FROM configs WHERE router_id=? ORDER BY timestamp DESC", (rid,)).fetchall()
            if len(cfgs) <= s["latest"]: continue
            keep = {c["id"] for c in cfgs[:s["latest"]]}
            now = datetime.utcnow()
            kept_days, kept_weeks, kept_months = set(), set(), set()
            for c in cfgs:
                try: 
                    ts = datetime.fromisoformat(c["timestamp"].replace("Z", "+00:00")).replace(tzinfo=None)
                    if ts > now - timedelta(days=s["days"]):
                        day = ts.strftime("%Y-%m-%d"); 
                        if day not in kept_days: keep.add(c["id"]); kept_days.add(day)
                    if ts > now - timedelta(weeks=s.get("weeks", 26)):
                        wk = ts.strftime("%Y-W%W");
                        if wk not in kept_weeks: keep.add(c["id"]); kept_weeks.add(wk)
                    mo = ts.strftime("%Y-%m")
                    if mo not in kept_months: keep.add(c["id"]); kept_months.add(mo)
                except: keep.add(c["id"])
            d_ids = [c["id"] for c in cfgs if c["id"] not in keep]
            if d_ids: conn.execute(f"DELETE FROM configs WHERE id IN ({','.join(['?']*len(d_ids))})", d_ids)
        
        cutoff = (datetime.utcnow() - timedelta(days=s["events"])).isoformat()
        conn.execute("DELETE FROM events WHERE timestamp < ?", (cutoff,))
        conn.execute("DELETE FROM system_logs WHERE timestamp < ?", (cutoff,))
        try: conn.execute("VACUUM")
        except: pass

# ========================================
# MODULE: src/core/bootstrap.py
# ========================================

BOOTSTRAP_TEMPLATES = {
    "cisco_ios": """! === RBRCS Bootstrap ===
hostname {{name}}
interface GigabitEthernet0/0
 ip address {{host}} 255.255.255.0
 no shut
username admin priv 15 secret admin
ip ssh version 2
line vty 0 4
 login local
 transport input ssh
""",
    "mikrotik_routeros": "/ip address add address={{host}}/24 interface=ether1\\n/ip service set ssh port=22 disabled=no"
}

def get_bootstrap_config(rid):
    r = get_router(rid)
    if not r: return None
    tpl = BOOTSTRAP_TEMPLATES.get(r["device_type"], "No template")
    for k, v in r.items(): tpl = tpl.replace(f"{{{{{k}}}}}", str(v))
    return tpl


def get_retention_stats():
    with db_conn() as conn:
        r = conn.execute("SELECT COUNT(*), COUNT(DISTINCT config_hash), SUM(LENGTH(config_data)), MIN(timestamp) FROM configs").fetchone()
        return {"total": r[0], "unique": r[1], "storage": r[2] or 0, "oldest": r[3]}


# ========================================
# MODULE: src/core/health.py
# ========================================
"""
health.py — Concise health tracking for RBRCS.
"""

logger = logging.getLogger("rbrcs.health")
ssh = SSHManager(); _state = {} # rid -> {"retries": 0, "status": "unknown"}

def check_single_router(r):
    rid = r["id"]; prev = r.get("status", "unknown"); s = _state.setdefault(rid, {"retries": 0})
    
    if not ssh.ping(r):
        s["retries"] += 1
        if s["retries"] >= 3 and prev != "offline":
            log_event(rid, "status_offline", "Unreachable after 3 retries", "warning")
            send_alert(r, "offline", f"⚠️ OFFLINE: {r['name']} ({r['host']})")
            update_router_status(rid, "offline")
        return {"id": rid, "status": "offline" if s["retries"] >= 3 else prev}

    s["retries"] = 0
    ok, msg = ssh.test_connection(r)
    if not ok:
        if prev != "ssh_error":
            log_event(rid, "ssh_error", msg, "warning"); send_alert(r, "ssh_error", f"⚠️ SSH Fail: {r['name']}")
            update_router_status(rid, "ssh_error")
        return {"id": rid, "status": "ssh_error"}

    update_router_status(rid, "online")
    if prev in ("offline", "ssh_error"):
        log_event(rid, "status_online", "Back online", "info"); send_alert(r, "back_online", f"✅ ONLINE: {r['name']}")
        check_and_restore(rid)
    return {"id": rid, "status": "online"}

def check_all_routers(): return [check_single_router(r) for r in get_all_routers()]


# ========================================
# MODULE: src/utils/syslog.py
# ========================================
"""
syslog.py — Concise syslog listener for RBRCS.
"""

logger = logging.getLogger("rbrcs.syslog")
PATTERNS = [re.compile(p, re.I) for p in [r"SYS-5-CONFIG_I", r"WRITE_MEM", r"config changed", r"system,info.*changed"]]

class SyslogHandler(socketserver.BaseRequestHandler):
    def handle(self):
        data = self.request[0].decode(errors="replace"); ip = self.client_address[0]
        if any(p.search(data) for p in PATTERNS):
            for r in get_all_routers():
                if r["host"] == ip:
                    log_event(r["id"], "syslog_trigger", f"Change detected: {data[:100]}")
                    backup_router(r["id"], "syslog")

class SyslogListener:
    def __init__(self, host="0.0.0.0", port=514): self.host, self.port = host, port
    def start(self):
        try:
            self.server = socketserver.UDPServer((self.host, self.port), SyslogHandler)
            threading.Thread(target=self.server.serve_forever, daemon=True).start()
            logger.info(f"Syslog live on {self.port}")
        except Exception as e: logger.error(f"Syslog fail: {e}")


# ========================================
# MODULE: src/scheduler.py
# ========================================
"""
scheduler.py — Concise APScheduler orchestration for RBRCS.
"""

logger = logging.getLogger("rbrcs.scheduler")
sched = None

def _drift():
    ssh = SSHManager()
    for r in get_all_routers():
        if r.get("status") == "online" and get_golden_config(r["id"]):
            try:
                curr = ssh.fetch_config(r)
                if curr: check_drift(r["id"], curr, r["device_type"])
            except: pass

def start_scheduler(cfg):
    global sched
    sched = BackgroundScheduler(daemon=True)
    h_int = cfg.get("health_check", {}).get("interval_minutes", 10)
    b_int = cfg.get("health_check", {}).get("backup_interval_minutes", 30)
    cl_hr = cfg.get("retention", {}).get("cleanup_hour", 3)

    sched.add_job(check_all_routers, "interval", minutes=h_int, id="health")
    sched.add_job(_drift, "interval", hours=1, id="drift")
    sched.add_job(run_retention_cleanup, "cron", hour=cl_hr, args=[cfg.get("retention")], id="cleanup")
    
    for r in cfg.get("routers", []):
        ri = r.get("backup_interval_minutes", b_int)
        sched.add_job(backup_router, "interval", minutes=int(ri), args=[r["id"], "poll"], id=f"bkp_{r['id']}")

    print("DEBUG: jobs added", flush=True)

def stop_scheduler(): sched.shutdown(wait=False)
def get_scheduled_jobs(): return [{"id": j.id, "name": j.name, "next": str(j.next_run_time)} for j in sched.get_jobs()]


# ========================================
# MODULE: src/web/server.py
# ========================================
"""
server.py — Concise Flask API for RBRCS.
"""


logger = logging.getLogger("rbrcs.web")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "src", "static")
app = Flask(__name__, static_folder=STATIC_DIR)

# ── Load Config ────────────────────────────────────────────
CONFIG_PATH = "config.yaml"
def load_config():
    if not os.path.exists(CONFIG_PATH): return {}
    with open(CONFIG_PATH, "r") as f: return yaml.safe_load(os.path.expandvars(f.read()))

config = load_config()
app.secret_key = config.get("system", {}).get("secret_key", "default")

@app.route("/")
def serve_index():
    return send_from_directory(STATIC_DIR, "index.html")

@app.route("/api/stats")
def api_stats():
    return jsonify(get_dashboard_stats())

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

@app.route("/api/routers/<rid>/history")
def api_history(rid):
    return jsonify(get_config_history(rid))

@app.route("/api/backup/<rid>")
def api_bkp(rid):
    return jsonify(backup_router(rid, "manual"))

@app.route("/api/restore/<rid>")
def api_res(rid):
    return jsonify(restore_router(rid, request.args.get("config_id", type=int)))

@app.route("/api/jobs")
def api_jobs():
    return jsonify(get_scheduled_jobs())

@app.route("/api/retention-stats")
def api_ret():
    return jsonify(get_retention_stats())


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

@app.route("/api/logs")
def api_logs():
    def generate():
        for log in memory_handler.get_logs()[-50:]: yield f"data: {log}\n\n"
        last_count = len(memory_handler.get_logs())
        while True:
            time.sleep(0.5)
            curr = memory_handler.get_logs()
            if len(curr) > last_count:
                for i in range(last_count, len(curr)): yield f"data: {curr[i]}\n\n"
                last_count = len(curr)
    return Response(generate(), mimetype="text/event-stream")

@app.route("/api/export-all")
def api_export_all():
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, 'w', zipfile.ZIP_DEFLATED) as zf:
        for r in get_all_routers():
            cfg = get_config(rid=r["id"])
            if cfg: zf.writestr(f"{r['name']}_{cfg['timestamp']}.txt".replace(" ", "_"), cfg["config_text"])
    mem.seek(0)
    return send_file(mem, mimetype="application/zip", as_attachment=True, download_name="rbrcs_export.zip")

@app.route("/api/import-routers", methods=["POST"])
def api_import_routers():
    if 'file' not in request.files: return jsonify({"success": False, "msg": "No file"}), 400
    file = request.files['file']
    try:
        df = pd.read_csv(file) if file.filename.endswith('.csv') else pd.read_excel(file)
        for _, row in df.iterrows():
            upsert_router({k: str(row.get(k, '')) for k in ['id', 'name', 'host', 'port', 'device_type', 'username', 'password']})
        return jsonify({"success": True})
    except Exception as e: return jsonify({"success": False, "msg": str(e)}), 500

@app.route("/api/bootstrap/<rid>")
def api_boot(rid):
    cfg = get_bootstrap_config(rid)
    return jsonify({"success": True, "config": cfg}) if cfg else jsonify({"success": False}), 404

def run():
    try:
        init_db()
        for r in config.get("routers", []): upsert_router(r)
        print("DEBUG: calling start_scheduler", flush=True)
        start_scheduler(config)
        print("DEBUG: returned from start_scheduler, starting sched now", flush=True)
        sched.start()
        print("DEBUG: sched.start() done", flush=True)
        
        syslog_cfg = config.get("syslog", {})
        if syslog_cfg.get("enabled"):
            print("DEBUG: starting syslog listener", flush=True)
            SyslogListener(syslog_cfg.get("host", "0.0.0.0"), syslog_cfg.get("port", 514)).start()
        
        port = config.get("system", {}).get("web_port", 8080)
        print(f"DEBUG: finally reaching serve on {port}", flush=True)
        if config.get("system", {}).get("debug", False):
            app.run(host="0.0.0.0", port=port, debug=True, use_reloader=False)
        else:
            logger.info(f"Starting Waitress on {port}")
            serve(app, host="0.0.0.0", port=port, threads=4)
    except Exception as e:
        print(f"CRITICAL: Application failed to start: {e}", flush=True)
        logger.critical(f"Startup crash: {e}", exc_info=True)
        sys.exit(1)



# ========================================
# MODULE: main.py
# ========================================
"""
main.py — Entry point for the RBRCS application.
"""

if __name__ == "__main__":
    run()