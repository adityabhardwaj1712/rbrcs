"""
database.py — Streamlined SQLite backend for RBRCS.
"""
import sqlite3, hashlib, zlib, os, logging
from contextlib import contextmanager

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
