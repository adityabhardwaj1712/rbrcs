"""
database.py — SQLite schema and helper functions for RBRCS.

Tables:
  - routers: registered router devices
  - configs: backed-up configurations (compressed, hash-deduplicated)
  - events:  audit log of all system activity
"""

import sqlite3
import hashlib
import zlib
import os
import logging
from datetime import datetime, timedelta


class SQLiteHandler(logging.Handler):
    """Custom logging handler to write system logs into the SQLite database."""
    def __init__(self, db_path=None):
        super().__init__()
        self.db_path = db_path

    def emit(self, record):
        try:
            conn = get_db(self.db_path)
            conn.execute("""
                INSERT INTO system_logs (logger, level, message)
                VALUES (?, ?, ?)
            """, (record.name, record.levelname, self.format(record)))
            conn.commit()
            conn.close()
        except Exception:
            # Silent failure to avoid recursive logging issues or blocking
            pass


DB_PATH = "rbrcs.db"


def get_db(db_path=None):
    """Get a database connection with row_factory enabled."""
    path = db_path or DB_PATH
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")      # Better concurrent access
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA auto_vacuum = FULL")    # Reclaim space when rows are deleted
    conn.execute("PRAGMA cache_size = -2000")    # 2MB cache, reduces disk I/O
    conn.execute("PRAGMA temp_store = MEMORY")   # Keep temporary tables/indices in memory
    return conn


def init_db(db_path=None):
    """Create all tables if they don't exist."""
    conn = get_db(db_path)
    cursor = conn.cursor()

    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS routers (
            id            TEXT PRIMARY KEY,
            name          TEXT NOT NULL,
            host          TEXT NOT NULL,
            port          INTEGER DEFAULT 22,
            device_type   TEXT NOT NULL,
            username      TEXT,
            password      TEXT,
            ssh_key_path  TEXT,
            enable_password TEXT,
            status        TEXT DEFAULT 'unknown',
            last_seen     DATETIME,
            last_backup   DATETIME,
            created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS configs (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            router_id     TEXT NOT NULL,
            config_hash   TEXT NOT NULL,
            config_data   BLOB NOT NULL,
            config_size   INTEGER NOT NULL,
            change_type   TEXT DEFAULT 'auto',
            timestamp     DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (router_id) REFERENCES routers(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_configs_router_time
            ON configs(router_id, timestamp DESC);

        CREATE INDEX IF NOT EXISTS idx_configs_hash
            ON configs(config_hash);

        CREATE TABLE IF NOT EXISTS events (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            router_id     TEXT,
            event_type    TEXT NOT NULL,
            message       TEXT,
            severity      TEXT DEFAULT 'info',
            timestamp     DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (router_id) REFERENCES routers(id) ON DELETE SET NULL
        );

        CREATE INDEX IF NOT EXISTS idx_events_time
            ON events(timestamp DESC);

        CREATE TABLE IF NOT EXISTS system_logs (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            logger        TEXT,
            level         TEXT,
            message       TEXT,
            timestamp     DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_system_logs_time
            ON system_logs(timestamp DESC);
    """)

    conn.commit()
    conn.close()


# ── Router CRUD ────────────────────────────────────────────

def upsert_router(router_dict, db_path=None):
    """Insert or update a router record from config.yaml data."""
    conn = get_db(db_path)
    conn.execute("""
        INSERT INTO routers (id, name, host, port, device_type, username, password,
                             ssh_key_path, enable_password)
        VALUES (:id, :name, :host, :port, :device_type, :username, :password,
                :ssh_key_path, :enable_password)
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name, host=excluded.host, port=excluded.port,
            device_type=excluded.device_type, username=excluded.username,
            password=excluded.password, ssh_key_path=excluded.ssh_key_path,
            enable_password=excluded.enable_password
    """, {
        "id": router_dict["id"],
        "name": router_dict.get("name", router_dict["id"]),
        "host": router_dict["host"],
        "port": router_dict.get("port", 22),
        "device_type": router_dict["device_type"],
        "username": router_dict.get("username", ""),
        "password": router_dict.get("password", ""),
        "ssh_key_path": router_dict.get("ssh_key_path", ""),
        "enable_password": router_dict.get("enable_password", ""),
    })
    conn.commit()
    conn.close()


def get_all_routers(db_path=None):
    """Return all routers."""
    conn = get_db(db_path)
    rows = conn.execute("SELECT * FROM routers ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_router(router_id, db_path=None):
    """Return a single router by ID."""
    conn = get_db(db_path)
    row = conn.execute("SELECT * FROM routers WHERE id = ?", (router_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_router(router_id, db_path=None):
    """Delete a router and all its configs/events."""
    conn = get_db(db_path)
    conn.execute("DELETE FROM configs WHERE router_id = ?", (router_id,))
    conn.execute("DELETE FROM events WHERE router_id = ?", (router_id,))
    conn.execute("DELETE FROM routers WHERE id = ?", (router_id,))
    conn.commit()
    conn.close()



def update_router_status(router_id, status, db_path=None):
    """Update router online/offline status."""
    conn = get_db(db_path)
    conn.execute("""
        UPDATE routers SET status = ?, last_seen = CURRENT_TIMESTAMP
        WHERE id = ?
    """, (status, router_id))
    conn.commit()
    conn.close()


# ── Config Storage ─────────────────────────────────────────

def compute_hash(config_text):
    """Compute MD5 hash of config text."""
    return hashlib.md5(config_text.encode("utf-8")).hexdigest()


def compress_config(config_text):
    """Compress config text with zlib."""
    return zlib.compress(config_text.encode("utf-8"), level=6)


def decompress_config(config_data):
    """Decompress stored config data."""
    return zlib.decompress(config_data).decode("utf-8")


def store_config(router_id, config_text, change_type="auto", db_path=None):
    """
    Store a config backup. Returns (config_id, is_new).
    If the hash matches the latest backup, skip storage (dedup).
    """
    config_hash = compute_hash(config_text)

    conn = get_db(db_path)

    # Check if latest config has the same hash (dedup)
    latest = conn.execute("""
        SELECT config_hash FROM configs
        WHERE router_id = ?
        ORDER BY timestamp DESC LIMIT 1
    """, (router_id,)).fetchone()

    if latest and latest["config_hash"] == config_hash:
        conn.close()
        return None, False  # No change, skip

    compressed = compress_config(config_text)
    cursor = conn.execute("""
        INSERT INTO configs (router_id, config_hash, config_data, config_size, change_type)
        VALUES (?, ?, ?, ?, ?)
    """, (router_id, config_hash, compressed, len(config_text), change_type))

    # Update router's last_backup timestamp
    conn.execute("""
        UPDATE routers SET last_backup = CURRENT_TIMESTAMP WHERE id = ?
    """, (router_id,))

    conn.commit()
    config_id = cursor.lastrowid
    conn.close()
    return config_id, True


def get_latest_config(router_id, db_path=None):
    """Get the most recent config for a router."""
    conn = get_db(db_path)
    row = conn.execute("""
        SELECT * FROM configs WHERE router_id = ?
        ORDER BY timestamp DESC LIMIT 1
    """, (router_id,)).fetchone()
    conn.close()
    if row:
        result = dict(row)
        result["config_text"] = decompress_config(result["config_data"])
        return result
    return None


def get_config_by_id(config_id, db_path=None):
    """Get a specific config by its ID."""
    conn = get_db(db_path)
    row = conn.execute("SELECT * FROM configs WHERE id = ?", (config_id,)).fetchone()
    conn.close()
    if row:
        result = dict(row)
        result["config_text"] = decompress_config(result["config_data"])
        return result
    return None


def get_config_history(router_id, limit=50, db_path=None):
    """Get backup history for a router."""
    conn = get_db(db_path)
    rows = conn.execute("""
        SELECT id, router_id, config_hash, config_size, change_type, timestamp
        FROM configs WHERE router_id = ?
        ORDER BY timestamp DESC LIMIT ?
    """, (router_id, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_config_count(router_id=None, db_path=None):
    """Count total configs, optionally filtered by router."""
    conn = get_db(db_path)
    if router_id:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM configs WHERE router_id = ?",
            (router_id,)
        ).fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) as cnt FROM configs").fetchone()
    conn.close()
    return row["cnt"]


def get_total_storage_bytes(db_path=None):
    """Get total storage used by all configs."""
    conn = get_db(db_path)
    row = conn.execute(
        "SELECT COALESCE(SUM(LENGTH(config_data)), 0) as total FROM configs"
    ).fetchone()
    conn.close()
    return row["total"]


# ── Event Logging ──────────────────────────────────────────

def log_event(router_id, event_type, message, severity="info", db_path=None):
    """Log a system event."""
    conn = get_db(db_path)
    conn.execute("""
        INSERT INTO events (router_id, event_type, message, severity)
        VALUES (?, ?, ?, ?)
    """, (router_id, event_type, message, severity))
    conn.commit()
    conn.close()


def get_events(limit=100, router_id=None, db_path=None):
    """Get recent events."""
    conn = get_db(db_path)
    if router_id:
        rows = conn.execute("""
            SELECT * FROM events WHERE router_id = ?
            ORDER BY timestamp DESC LIMIT ?
        """, (router_id, limit)).fetchall()
    else:
        rows = conn.execute("""
            SELECT * FROM events ORDER BY timestamp DESC LIMIT ?
        """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Statistics ─────────────────────────────────────────────

def get_dashboard_stats(db_path=None):
    """Get summary statistics for the dashboard."""
    conn = get_db(db_path)

    total_routers = conn.execute("SELECT COUNT(*) as cnt FROM routers").fetchone()["cnt"]
    online_routers = conn.execute(
        "SELECT COUNT(*) as cnt FROM routers WHERE status = 'online'"
    ).fetchone()["cnt"]
    total_backups = conn.execute("SELECT COUNT(*) as cnt FROM configs").fetchone()["cnt"]
    total_storage = conn.execute(
        "SELECT COALESCE(SUM(LENGTH(config_data)), 0) as total FROM configs"
    ).fetchone()["total"]

    # Recent events
    recent_events = conn.execute("""
        SELECT e.*, r.name as router_name
        FROM events e LEFT JOIN routers r ON e.router_id = r.id
        ORDER BY e.timestamp DESC LIMIT 20
    """).fetchall()

    # Last backup per router
    routers_with_stats = conn.execute("""
        SELECT r.*,
               (SELECT COUNT(*) FROM configs c WHERE c.router_id = r.id) as backup_count
        FROM routers r ORDER BY r.name
    """).fetchall()

    conn.close()

    return {
        "total_routers": total_routers,
        "online_routers": online_routers,
        "total_backups": total_backups,
        "total_storage": total_storage,
        "recent_events": [dict(e) for e in recent_events],
        "routers": [dict(r) for r in routers_with_stats],
    }
