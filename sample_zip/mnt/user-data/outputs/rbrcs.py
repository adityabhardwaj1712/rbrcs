"""
rbrcs.py — Lightweight Router Configuration Backup & Recovery System
Single-file version. All modules merged in dependency order.
Run: python rbrcs.py
"""

# ── Standard library ───────────────────────────────────────────────────
import difflib
import hashlib
import io
import logging
import os
import re
import socket
import socketserver
import sqlite3
import sys
import textwrap
import threading
import time
import zipfile
import zlib
from datetime import datetime, timedelta

# ── Third-party ────────────────────────────────────────────────────────
import paramiko
import requests
import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, Response, render_template, request, jsonify, redirect, url_for, send_file
from waitress import serve



# ======================================================================
# MODULE: database.py
# ======================================================================

DB_PATH = "rbrcs.db"


def get_db(db_path=None):
    """Get a database connection with row_factory enabled."""
    path = db_path or DB_PATH
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")      # Better concurrent access
    conn.execute("PRAGMA foreign_keys=ON")
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



# ======================================================================
# MODULE: golden_config.py
# ======================================================================

logger = logging.getLogger("rbrcs.golden")

# Critical config sections that MUST exist per device type.
# If any section disappears from a backup compared to golden → partial loss alert.
CRITICAL_SECTIONS = {
    "cisco_ios": [
        "hostname",
        "interface",
        "ip route",
        "line vty",
        "line con",
        "username",
    ],
    "mikrotik_routeros": [
        "/ip address",
        "/ip route",
        "/interface",
        "/system identity",
    ],
    "ubiquiti_edgeos": [
        "interfaces",
        "system",
        "protocols",
    ],
    "generic_linux": [
        "address",
        "gateway",
    ],
}


# ── Database Helpers ──────────────────────────────────────

def _ensure_table(db_path=None):
    """Create golden_configs table if it doesn't exist (lazy migration)."""
    conn = get_db(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS golden_configs (
            router_id     TEXT PRIMARY KEY,
            config_hash   TEXT NOT NULL,
            config_data   BLOB NOT NULL,
            config_size   INTEGER NOT NULL,
            promoted_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
            promoted_by   TEXT DEFAULT 'system',
            FOREIGN KEY (router_id) REFERENCES routers(id) ON DELETE CASCADE
        )
    """)
    conn.commit()
    conn.close()


def set_golden_config(router_id, config_text, promoted_by="admin", db_path=None):
    """
    Promote a config text as the golden/approved baseline for a router.
    Overwrites any existing golden config for that router.
    """
    _ensure_table(db_path)
    config_hash = compute_hash(config_text)
    compressed = compress_config(config_text)

    conn = get_db(db_path)
    conn.execute("""
        INSERT INTO golden_configs (router_id, config_hash, config_data, config_size, promoted_by)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(router_id) DO UPDATE SET
            config_hash=excluded.config_hash,
            config_data=excluded.config_data,
            config_size=excluded.config_size,
            promoted_at=CURRENT_TIMESTAMP,
            promoted_by=excluded.promoted_by
    """, (router_id, config_hash, compressed, len(config_text), promoted_by))
    conn.commit()
    conn.close()

    log_event(router_id, "golden_config_set",
              f"Golden config promoted ({len(config_text)} bytes, by {promoted_by})",
              "info", db_path)
    logger.info(f"Golden config set for router {router_id} by {promoted_by}")


def get_golden_config(router_id, db_path=None):
    """
    Get the golden config for a router. Returns dict or None.
    """
    _ensure_table(db_path)
    conn = get_db(db_path)
    row = conn.execute(
        "SELECT * FROM golden_configs WHERE router_id = ?", (router_id,)
    ).fetchone()
    conn.close()

    if row:
        result = dict(row)
        result["config_text"] = decompress_config(result["config_data"])
        return result
    return None


def delete_golden_config(router_id, db_path=None):
    """Remove the golden config for a router."""
    _ensure_table(db_path)
    conn = get_db(db_path)
    conn.execute("DELETE FROM golden_configs WHERE router_id = ?", (router_id,))
    conn.commit()
    conn.close()
    log_event(router_id, "golden_config_removed", "Golden config removed", "info", db_path)


# ── Drift Detection ──────────────────────────────────────

def check_drift(router_id, current_config_text, device_type, db_path=None):
    """
    Compare a new config against the golden baseline.
    Returns a drift report dict, or None if no golden config is set.

    Drift report:
      - has_drift: bool
      - additions: int (lines added)
      - deletions: int (lines removed)
      - missing_sections: list of critical sections not found
      - summary: human-readable string
    """
    golden = get_golden_config(router_id, db_path)
    if not golden:
        return None  # No golden config set — skip drift check

    golden_text = golden["config_text"]
    golden_hash = golden["config_hash"]
    current_hash = compute_hash(current_config_text)

    # Quick check: if hashes match, no drift
    if current_hash == golden_hash:
        return {"has_drift": False, "additions": 0, "deletions": 0,
                "missing_sections": [], "summary": "Config matches golden baseline"}

    # ── Compute line-level diff ───────────────────────────
    golden_lines = golden_text.splitlines()
    current_lines = current_config_text.splitlines()
    diff = list(difflib.unified_diff(golden_lines, current_lines, n=0))

    additions = sum(1 for l in diff if l.startswith('+') and not l.startswith('+++'))
    deletions = sum(1 for l in diff if l.startswith('-') and not l.startswith('---'))

    # ── Check for missing critical sections (partial loss) ──
    sections = CRITICAL_SECTIONS.get(device_type, [])
    current_lower = current_config_text.lower()
    missing = [s for s in sections if s.lower() not in current_lower]

    summary_parts = []
    if additions or deletions:
        summary_parts.append(f"+{additions}/-{deletions} lines changed from golden")
    if missing:
        summary_parts.append(f"MISSING sections: {', '.join(missing)}")

    summary = " | ".join(summary_parts) if summary_parts else "Minor drift detected"

    # Log if there's meaningful drift
    severity = "warning"
    if missing:
        severity = "error"
        log_event(router_id, "partial_config_loss",
                  f"Critical sections missing vs golden: {', '.join(missing)}",
                  "error", db_path)

    if additions > 0 or deletions > 0:
        log_event(router_id, "config_drift",
                  f"Drift from golden: {summary}", severity, db_path)

    return {
        "has_drift": True,
        "additions": additions,
        "deletions": deletions,
        "missing_sections": missing,
        "summary": summary,
    }


def check_corruption(config_text, device_type):
    """
    Basic heuristic to detect corrupted/garbled config text.
    Returns (is_corrupted: bool, reason: str).

    SOLVES: Problem #8 (Corrupted Configuration)
    """
    if not config_text:
        return True, "Config is empty"

    # Check for high ratio of non-printable / garbage characters
    total = len(config_text)
    non_printable = sum(1 for c in config_text if not c.isprintable() and c not in '\n\r\t')
    if total > 0 and (non_printable / total) > 0.05:
        return True, f"Config has {non_printable} non-printable chars ({non_printable*100//total}%)"

    # Check for common corruption patterns
    if config_text.count('\x00') > 5:
        return True, "Config contains null bytes (memory corruption)"

    # Check for truncated config (ends mid-line without proper termination)
    lines = config_text.strip().splitlines()
    if lines:
        last_line = lines[-1].strip()
        # Cisco configs should end with 'end'
        if device_type == "cisco_ios" and len(lines) > 20:
            if last_line and not last_line.startswith("end") and not last_line.startswith("!"):
                return True, f"Config appears truncated (last line: '{last_line[:40]}')"

    return False, "Config appears valid"



# ======================================================================
# MODULE: ssh_manager.py
# ======================================================================

logger = logging.getLogger("rbrcs.ssh")


# Default commands per device type
DEFAULT_COMMANDS = {
    "cisco_ios": ["terminal length 0", "show running-config"],
    "mikrotik_routeros": ["/export"],
    "ubiquiti_edgeos": ["show configuration"],
    "generic_linux": ["cat /etc/network/interfaces"],
}

# Config push commands per device type
RESTORE_PREAMBLE = {
    "cisco_ios": ["configure terminal"],
    "mikrotik_routeros": [],
    "ubiquiti_edgeos": ["configure"],
    "generic_linux": [],
}

RESTORE_POSTAMBLE = {
    "cisco_ios": ["end", "write memory"],
    "mikrotik_routeros": [],
    "ubiquiti_edgeos": ["commit", "save", "exit"],
    "generic_linux": [],
}


class SSHManager:
    """Manages SSH connections to routers with pooling."""
    _pool = {}  # router_id -> (client, timestamp)

    def __init__(self, timeout=None):
        # timeout resolved lazily on first use so module-level instantiation works
        # before config is loaded
        self._timeout_override = timeout

    @property
    def timeout(self):
        if self._timeout_override is not None:
            return self._timeout_override
        try:
            return config.get("health_check", {}).get("ping_timeout_seconds", 30)
        except Exception:
            return 30

    def _connect(self, router):
        """Create an SSH connection to a router or return a cached one."""
        router_id = router.get("id")
        
        if router_id in self._pool:
            client, last_used = self._pool[router_id]
            if time.time() - last_used < 300:  # 5 minutes TTL
                try:
                    transport = client.get_transport()
                    if transport and transport.is_active():
                        self._pool[router_id] = (client, time.time())
                        return client
                except Exception:
                    pass
            # Stale or dead connection, clean up
            try:
                client.close()
            except Exception:
                pass
            del self._pool[router_id]

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs = {
            "hostname": router["host"],
            "port": router.get("port", 22),
            "username": router.get("username", "admin"),
            "timeout": self.timeout,
            "look_for_keys": False,
            "allow_agent": False,
        }

        # Use SSH key if specified, otherwise password
        ssh_key_path = router.get("ssh_key_path", "")
        if ssh_key_path:
            connect_kwargs["key_filename"] = ssh_key_path
        else:
            connect_kwargs["password"] = router.get("password", "")

        client.connect(**connect_kwargs)
        
        if router_id:
            self._pool[router_id] = (client, time.time())
            
        return client

    def test_connection(self, router):
        """Test if a router is reachable via SSH. Returns (success, message)."""
        try:
            client = self._connect(router)
            # We don't close it, leave it in pool
            return True, "Connection successful"
        except paramiko.AuthenticationException:
            return False, "Authentication failed"
        except paramiko.SSHException as e:
            return False, f"SSH error: {e}"
        except socket.timeout:
            return False, "Connection timed out"
        except socket.error as e:
            return False, f"Network error: {e}"
        except Exception as e:
            return False, f"Unknown error: {e}"

    def ping(self, router):
        """Quick TCP check if SSH port is open."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            result = sock.connect_ex((router["host"], router.get("port", 22)))
            sock.close()
            return result == 0
        except Exception:
            return False

    def fetch_config(self, router, custom_commands=None):
        """
        SSH into a router and fetch its running configuration.
        Returns the config text as a string.
        """
        device_type = router.get("device_type", "cisco_ios")
        commands = custom_commands or router.get(
            "backup_commands",
            DEFAULT_COMMANDS.get(device_type, ["show running-config"])
        )

        client = None
        try:
            client = self._connect(router)

            if device_type == "cisco_ios":
                return self._fetch_cisco(client, router, commands)
            else:
                return self._fetch_generic(client, commands)

        finally:
            # Pooled connection will remain open
            pass

    def _fetch_cisco(self, client, router, commands):
        """Fetch config from Cisco IOS using an interactive shell (handles enable mode)."""
        shell = client.invoke_shell()
        time.sleep(1)
        shell.recv(65535)  # Clear banner

        # Enter enable mode if needed
        enable_pwd = router.get("enable_password", "")
        if enable_pwd:
            shell.send("enable\n")
            time.sleep(1)
            shell.recv(65535)
            shell.send(enable_pwd + "\n")
            time.sleep(1)
            shell.recv(65535)

        output_parts = []
        for cmd in commands:
            shell.send(cmd + "\n")
            time.sleep(2)
            while shell.recv_ready():
                chunk = shell.recv(65535).decode("utf-8", errors="replace")
                output_parts.append(chunk)
                time.sleep(0.5)

        shell.close()
        full_output = "".join(output_parts)

        # Clean up Cisco output (remove command echo, prompts)
        lines = full_output.split("\n")
        clean_lines = []
        capture = False
        for line in lines:
            stripped = line.strip()
            if "show running-config" in stripped:
                capture = True
                continue
            if capture:
                # Stop at the next prompt (hostname#)
                if stripped.endswith("#") and len(stripped) < 60:
                    break
                clean_lines.append(line.rstrip())

        return "\n".join(clean_lines)

    def _fetch_generic(self, client, commands):
        """Fetch config using exec commands (MikroTik, Ubiquiti, Linux)."""
        output_parts = []
        for cmd in commands:
            stdin, stdout, stderr = client.exec_command(cmd, timeout=30)
            output = stdout.read().decode("utf-8", errors="replace")
            output_parts.append(output)
        return "\n".join(output_parts)

    def push_config(self, router, config_text):
        """
        Push a configuration to a router.
        Returns (success, message).
        """
        device_type = router.get("device_type", "cisco_ios")
        client = None

        try:
            client = self._connect(router)

            if device_type == "cisco_ios":
                return self._push_cisco(client, router, config_text)
            elif device_type == "mikrotik_routeros":
                return self._push_mikrotik(client, config_text)
            else:
                return self._push_generic(client, config_text)

        except Exception as e:
            return False, f"Restore failed: {e}"
        finally:
            pass

    def execute_commands(self, router, commands_text):
        """
        Execute arbitrary commands/configurations and RETURN the complete terminal output.
        Designed for dynamic UI ad-hoc configuration execution.
        """
        device_type = router.get("device_type", "cisco_ios")
        client = None

        try:
            client = self._connect(router)
            
            if device_type == "cisco_ios":
                shell = client.invoke_shell()
                time.sleep(1)
                shell.recv(65535)

                enable_pwd = router.get("enable_password", "")
                if enable_pwd:
                    shell.send("enable\n")
                    time.sleep(1)
                    shell.recv(65535)
                    shell.send(enable_pwd + "\n")
                    time.sleep(1)
                    shell.recv(65535)

                shell.send("configure terminal\n")
                time.sleep(1)
                
                output_parts = []
                for line in commands_text.split("\n"):
                    line = line.strip()
                    if not line: continue
                    shell.send(line + "\n")
                    time.sleep(0.5)
                    while shell.recv_ready():
                        output_parts.append(shell.recv(65535).decode("utf-8", errors="replace"))

                shell.send("end\n")
                time.sleep(1)
                while shell.recv_ready():
                    output_parts.append(shell.recv(65535).decode("utf-8", errors="replace"))
                
                shell.close()
                return True, "".join(output_parts)
                
            else:
                output_parts = []
                for line in commands_text.split("\n"):
                    if not line.strip(): continue
                    stdin, stdout, stderr = client.exec_command(line.strip(), timeout=30)
                    output_parts.append(stdout.read().decode("utf-8", errors="replace"))
                    err = stderr.read().decode("utf-8", errors="replace")
                    if err:
                        output_parts.append(err)
                return True, "\n".join(output_parts)

        except Exception as e:
            return False, f"Execution failed: {str(e)}"
        finally:
            pass

    def _push_cisco(self, client, router, config_text):
        """Push config to Cisco IOS line-by-line."""
        shell = client.invoke_shell()
        time.sleep(1)
        shell.recv(65535)

        # Enable mode
        enable_pwd = router.get("enable_password", "")
        if enable_pwd:
            shell.send("enable\n")
            time.sleep(1)
            shell.recv(65535)
            shell.send(enable_pwd + "\n")
            time.sleep(1)
            shell.recv(65535)

        # Enter config mode
        shell.send("configure terminal\n")
        time.sleep(1)
        shell.recv(65535)

        # Send config line by line
        for line in config_text.split("\n"):
            line = line.strip()
            if not line or line.startswith("!") or line.startswith("Building"):
                continue
            if line.startswith("Current configuration"):
                continue
            if line == "end":
                continue
            shell.send(line + "\n")
            time.sleep(0.1)

        time.sleep(2)
        shell.recv(65535)

        # Exit and save
        shell.send("end\n")
        time.sleep(1)
        shell.send("write memory\n")
        time.sleep(3)
        output = shell.recv(65535).decode("utf-8", errors="replace")
        shell.close()

        if "OK" in output or "bytes copied" in output or "[OK]" in output:
            return True, "Configuration restored and saved"
        return True, "Configuration pushed (verify manually)"

    def _push_mikrotik(self, client, config_text):
        """Push config to MikroTik via import."""
        # Upload config as a file, then import
        sftp = client.open_sftp()
        sftp.open("/tmp/restore.rsc", "w").write(config_text)
        sftp.close()

        stdin, stdout, stderr = client.exec_command("/import file=/tmp/restore.rsc")
        output = stdout.read().decode("utf-8", errors="replace")
        errors = stderr.read().decode("utf-8", errors="replace")

        if errors:
            return False, f"Import errors: {errors}"
        return True, "Configuration imported"

    def _push_generic(self, client, config_text):
        """Generic restore — write to file."""
        sftp = client.open_sftp()
        with sftp.open("/tmp/restored_config.txt", "w") as f:
            f.write(config_text)
        sftp.close()
        return True, "Config saved to /tmp/restored_config.txt on device"



# ======================================================================
# MODULE: compliance.py
# ======================================================================

logger = logging.getLogger("rbrcs.compliance")

# ── Cisco IOS Rules ────────────────────────────────────────

CISCO_RULES = [
    {
        "id": "C-01", "severity": "error",
        "description": "Plaintext enable password (use 'enable secret' instead)",
        "trigger": lambda l: l.strip().startswith("enable password") and "secret" not in l,
    },
    {
        "id": "C-02", "severity": "warning",
        "description": "Weak Type 7 password encryption detected",
        "trigger": lambda l: "password 7 " in l,
    },
    {
        "id": "C-03", "severity": "warning",
        "description": "Password encryption service is disabled",
        "trigger": lambda l: l.strip() == "no service password-encryption",
    },
    {
        "id": "C-04", "severity": "error",
        "description": "Telnet (VTY) access without ACL — anyone can connect",
        "trigger": lambda l: l.strip() == "transport input telnet",
    },
    {
        "id": "C-05", "severity": "warning",
        "description": "HTTP server enabled (security risk on production router)",
        "trigger": lambda l: l.strip() == "ip http server",
    },
    {
        "id": "C-06", "severity": "error",
        "description": "No SSH configured — management traffic is unencrypted",
        "trigger": lambda l: l.strip() == "transport input none",
    },
    {
        "id": "C-07", "severity": "warning",
        "description": "CDP enabled globally (information leakage risk)",
        "trigger": lambda l: l.strip() == "cdp run",
    },
    {
        "id": "C-08", "severity": "warning",
        "description": "No logging configured — events will be lost",
        "trigger": lambda l: l.strip() == "no logging console",
    },
]

# ── MikroTik Rules ─────────────────────────────────────────

MIKROTIK_RULES = [
    {
        "id": "M-01", "severity": "error",
        "description": "Default admin user with empty password",
        "trigger": lambda l: "/user add name=admin" in l and "password=" not in l,
    },
    {
        "id": "M-02", "severity": "warning",
        "description": "Winbox service on default port (security risk)",
        "trigger": lambda l: "/ip service set winbox" in l and "disabled=yes" not in l,
    },
]

# ── Section Presence Check (Partial Loss Detection) ────────

REQUIRED_SECTIONS = {
    "cisco_ios": {
        "service timestamps": "Logging timestamps are missing",
        "logging": "No syslog/logging configured",
        "ntp": "No NTP time sync configured",
        "banner": "No login banner (compliance requirement)",
    },
    "mikrotik_routeros": {
        "/system ntp": "No NTP configured",
        "/ip firewall": "No firewall rules defined",
    },
}


def run_compliance_check(router_id, device_type, config_text, db_path=None):
    """
    Run all compliance rules + section presence checks.
    Lightweight: pure string operations, no regex, no external libs.
    """
    if not config_text:
        return

    # ── Rule-based checks ─────────────────────────────────
    rules = []
    if device_type == "cisco_ios":
        rules = CISCO_RULES
    elif device_type == "mikrotik_routeros":
        rules = MIKROTIK_RULES

    lines = config_text.splitlines()
    triggered = set()

    for line in lines:
        for rule in rules:
            if rule["id"] not in triggered and rule["trigger"](line):
                triggered.add(rule["id"])
                msg = (f"[{rule['id']}] {rule['description']} "
                       f"(line: '{line.strip()[:50]}')")
                log_event(router_id, "compliance_violation", msg,
                          rule["severity"], db_path)

    # ── Section presence checks ───────────────────────────
    sections = REQUIRED_SECTIONS.get(device_type, {})
    config_lower = config_text.lower()

    for keyword, description in sections.items():
        if keyword.lower() not in config_lower:
            msg = f"[SEC] Missing: {description} (keyword: '{keyword}')"
            log_event(router_id, "compliance_warning", msg, "warning", db_path)

    if not triggered:
        logger.debug(f"Router {router_id}: passed all compliance checks")



# ======================================================================
# MODULE: restore_engine.py
# ======================================================================

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
        url = config.get("alerts", {}).get("webhook_url", "")
        if url:
            requests.post(url, json={"text": message}, timeout=5)
    except Exception:
        pass



# ======================================================================
# MODULE: backup_engine.py
# ======================================================================

logger = logging.getLogger("rbrcs.backup")
ssh = SSHManager()


def backup_router(router_id, change_type="auto", db_path=None):
    """
    Back up a single router's configuration.
    Returns dict with keys: success, router_id, message, is_new, config_id
    """
    router = get_router(router_id, db_path)
    if not router:
        return {"success": False, "router_id": router_id,
                "message": "Router not found", "is_new": False, "config_id": None}

    logger.info(f"Backing up: {router['name']} ({router['host']})")

    try:
        # ── Step 1: Fetch config via SSH ──────────────────────
        config_text = ssh.fetch_config(router)

        # ── Step 2: Reject empty configs ──────────────────────
        if not config_text or len(config_text.strip()) < 10:
            msg = "Empty or invalid config received"
            logger.warning(f"{router['name']}: {msg}")
            log_event(router_id, "backup_warning", msg, "warning", db_path)
            update_router_status(router_id, "online", db_path)
            return {"success": False, "router_id": router_id,
                    "message": msg, "is_new": False, "config_id": None}

        # ── Step 3: Corruption check ──────────────────────────
        is_corrupt, corrupt_reason = check_corruption(config_text, router.get("device_type"))
        if is_corrupt:
            msg = f"Corrupted config detected: {corrupt_reason}"
            logger.error(f"{router['name']}: {msg}")
            log_event(router_id, "config_corrupted", msg, "error", db_path)
            _send_alert(router, "config_corrupted",
                        f"⚠️ CORRUPTED CONFIG on {router['name']}: {corrupt_reason}")
            update_router_status(router_id, "online", db_path)
            return {"success": False, "router_id": router_id,
                    "message": msg, "is_new": False, "config_id": None}

        # ── Step 4: Factory reset check ───────────────────────
        if is_factory_default(config_text, router.get("device_type")):
            msg = "Factory default config detected — triggering auto-restore"
            logger.warning(f"{router['name']}: {msg}")
            log_event(router_id, "factory_reset_detected", msg, "warning", db_path)
            _send_alert(router, "factory_reset_detected",
                        f"🚨 Factory reset on {router['name']} ({router['host']}). Auto-restoring.")

            restore_result = restore_router(router_id, db_path=db_path)
            restore_msg = restore_result.get("message", "unknown")
            if not restore_result.get("success"):
                _send_alert(router, "restore_failed",
                            f"❌ Auto-restore FAILED on {router['name']}: {restore_msg}")

            return {"success": False, "router_id": router_id,
                    "message": f"Factory reset intercepted — {restore_msg}",
                    "is_new": False, "config_id": None}

        # ── Step 5: Store config (with dedup) ─────────────────
        config_id, is_new = store_config(router_id, config_text, change_type, db_path)
        update_router_status(router_id, "online", db_path)

        if is_new:
            msg = f"New config saved (ID: {config_id}, {len(config_text)} bytes)"

            # Diff summary vs previous backup
            try:
                history = get_config_history(router_id, limit=2, db_path=db_path)
                if len(history) >= 2:
                    prev = get_config_by_id(history[1]["id"], db_path)
                    if prev:
                        d = list(difflib.unified_diff(
                            prev["config_text"].splitlines(),
                            config_text.splitlines(), n=0))
                        adds = sum(1 for l in d if l.startswith('+') and not l.startswith('+++'))
                        dels = sum(1 for l in d if l.startswith('-') and not l.startswith('---'))
                        msg += f" | Δ +{adds}/-{dels} lines"
            except Exception:
                pass

            logger.info(f"{router['name']}: {msg}")
            log_event(router_id, "backup_new", msg, "info", db_path)

            # ── Step 6: Golden config drift check ─────────────
            golden = get_golden_config(router_id, db_path)
            if golden:
                drift = check_drift(router_id, config_text,
                                    router.get("device_type"), db_path)
                if drift and drift["has_drift"]:
                    drift_msg = f"Config drift: {drift['summary']}"
                    severity = "error" if drift["missing_sections"] else "warning"
                    _send_alert(router, "config_drift",
                                f"⚠️ {router['name']}: {drift_msg}")
            else:
                # No golden config exists yet → auto-promote first good backup
                set_golden_config(router_id, config_text, "auto-first-backup", db_path)
                logger.info(f"{router['name']}: First backup auto-promoted as golden config")

            # ── Step 7: Compliance scan ───────────────────────
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
    """Back up all registered routers."""
    routers = get_all_routers(db_path)
    return [backup_router(r["id"], "auto", db_path) for r in routers]


def _send_alert(router, event_type, message):
    """Send webhook alert if configured."""
    try:
        url = config.get("alerts", {}).get("webhook_url", "")
        if url:
            requests.post(url, json={"text": f"🚨 RBRCS: {message}"}, timeout=5)
    except Exception:
        pass



# ======================================================================
# MODULE: retention.py
# ======================================================================

logger = logging.getLogger("rbrcs.retention")


def run_retention_cleanup(config=None, db_path=None):
    """
    Run smart retention on configs + auto-cleanup events.
    Called daily by scheduler.
    """
    settings = config or {
        "keep_latest": 10,
        "daily_keep_days": 30,
        "weekly_keep_weeks": 26,
        "event_retention_days": 30,
    }

    conn = get_db(db_path)

    # ── Config retention ──────────────────────────────────
    routers = conn.execute("SELECT DISTINCT id FROM routers").fetchall()
    total_deleted = 0

    for router_row in routers:
        deleted = _cleanup_router(conn, router_row["id"], settings)
        total_deleted += deleted

    # ── Event log cleanup ─────────────────────────────────
    event_days = settings.get("event_retention_days", 30)
    cutoff = (datetime.utcnow() - timedelta(days=event_days)).isoformat()
    result = conn.execute("DELETE FROM events WHERE timestamp < ?", (cutoff,))
    events_deleted = result.rowcount

    conn.commit()

    # ── VACUUM to reclaim disk space ──────────────────────
    if total_deleted > 0 or events_deleted > 0:
        try:
            conn.execute("VACUUM")
        except Exception:
            pass  # VACUUM can fail inside transaction, that's OK

    conn.close()

    if total_deleted > 0 or events_deleted > 0:
        logger.info(f"Retention: {total_deleted} configs + {events_deleted} events cleaned")
    else:
        logger.debug("Retention: nothing to clean")

    return total_deleted


def _cleanup_router(conn, router_id, settings):
    """Apply tiered retention to a single router's backups."""
    keep_latest = settings.get("keep_latest", 10)
    daily_days = settings.get("daily_keep_days", 30)
    weekly_weeks = settings.get("weekly_keep_weeks", 26)

    all_configs = conn.execute("""
        SELECT id, timestamp, config_hash FROM configs
        WHERE router_id = ? ORDER BY timestamp DESC
    """, (router_id,)).fetchall()

    if len(all_configs) <= keep_latest:
        return 0

    keep_ids = set()
    now = datetime.utcnow()

    # Rule 1: Always keep latest N
    for cfg in all_configs[:keep_latest]:
        keep_ids.add(cfg["id"])

    # Rule 2: Keep 1/day for last N days
    daily_cutoff = now - timedelta(days=daily_days)
    kept_days = set()
    for cfg in all_configs:
        try:
            ts = datetime.fromisoformat(cfg["timestamp"].replace("Z", "+00:00"))
            if ts >= daily_cutoff:
                day_key = ts.strftime("%Y-%m-%d")
                if day_key not in kept_days:
                    keep_ids.add(cfg["id"])
                    kept_days.add(day_key)
        except Exception:
            keep_ids.add(cfg["id"])  # Keep if timestamp is unparseable

    # Rule 3: Keep 1/week for last N weeks
    weekly_cutoff = now - timedelta(weeks=weekly_weeks)
    kept_weeks = set()
    for cfg in all_configs:
        try:
            ts = datetime.fromisoformat(cfg["timestamp"].replace("Z", "+00:00"))
            if ts >= weekly_cutoff:
                week_key = ts.strftime("%Y-W%W")
                if week_key not in kept_weeks:
                    keep_ids.add(cfg["id"])
                    kept_weeks.add(week_key)
        except Exception:
            pass

    # Rule 4: Keep 1/month forever
    kept_months = set()
    for cfg in all_configs:
        try:
            ts = datetime.fromisoformat(cfg["timestamp"].replace("Z", "+00:00"))
            month_key = ts.strftime("%Y-%m")
            if month_key not in kept_months:
                keep_ids.add(cfg["id"])
                kept_months.add(month_key)
        except Exception:
            pass

    # Delete what's not kept
    all_ids = {cfg["id"] for cfg in all_configs}
    delete_ids = all_ids - keep_ids

    if delete_ids:
        placeholders = ",".join("?" * len(delete_ids))
        conn.execute(f"DELETE FROM configs WHERE id IN ({placeholders})", list(delete_ids))

    return len(delete_ids)


def get_retention_stats(db_path=None):
    """Dashboard stats — single query, minimal I/O."""
    conn = get_db(db_path)

    row = conn.execute("""
        SELECT
            COUNT(*) as total,
            COUNT(DISTINCT config_hash) as unique_hashes,
            COALESCE(SUM(LENGTH(config_data)), 0) as storage,
            MIN(timestamp) as oldest
        FROM configs
    """).fetchone()

    conn.close()

    total = row["total"]
    unique = row["unique_hashes"]

    return {
        "total_backups": total,
        "unique_configs": unique,
        "duplicates_avoided": total - unique,
        "total_storage_bytes": row["storage"],
        "oldest_backup": row["oldest"],
    }



# ======================================================================
# MODULE: health_checker.py
# ======================================================================

logger = logging.getLogger("rbrcs.health")
ssh = SSHManager()

# ── State tracking (in-memory, zero storage cost) ──────────
_offline_alerted = set()           # routers that already got an offline alert
_retry_count = {}                  # router_id → consecutive failure count
_first_offline_time = {}           # router_id → timestamp when first went offline

MAX_RETRIES = 3                    # attempts before marking offline
LONG_OFFLINE_HOURS = 2             # alert again if offline > this long


def check_all_routers(db_path=None):
    """Check all registered routers. Returns list of result dicts."""
    routers = get_all_routers(db_path)
    results = [check_single_router(r, db_path) for r in routers]

    online = sum(1 for r in results if r["status"] == "online")
    offline = sum(1 for r in results if r["status"] == "offline")
    logger.info(f"Health check: {online} online, {offline} offline (total: {len(results)})")
    return results


def check_single_router(router, db_path=None):
    """
    Full health check with retry logic:
      1. TCP ping (fast) — if fails, increment retry counter
      2. If retries < MAX_RETRIES → don't mark offline yet (transient issue)
      3. If retries >= MAX_RETRIES → mark offline, send ONE alert
      4. If router comes back → check for factory reset
    """
    router_id = router["id"]
    previous_status = router.get("status", "unknown")

    # ── Step 1: TCP ping ──────────────────────────────────
    is_reachable = ssh.ping(router)

    if not is_reachable:
        # Increment retry counter
        _retry_count[router_id] = _retry_count.get(router_id, 0) + 1
        retries = _retry_count[router_id]

        if retries < MAX_RETRIES:
            # Transient failure — don't alert yet
            logger.debug(f"{router['name']}: Unreachable (retry {retries}/{MAX_RETRIES})")
            return {"router_id": router_id, "name": router["name"],
                    "status": previous_status, "retrying": True}

        # Confirmed offline after MAX_RETRIES attempts
        if previous_status != "offline":
            logger.warning(f"{router['name']}: OFFLINE after {retries} retries")
            log_event(router_id, "status_offline",
                      f"Unreachable on {router['host']}:{router.get('port', 22)} "
                      f"after {retries} retries", "warning", db_path)
            _send_alert(router, "offline",
                        f"⚠️ OFFLINE: {router['name']} ({router['host']}) unreachable")
            _offline_alerted.add(router_id)
            _first_offline_time[router_id] = time.time()

        # Check if long-offline alert is needed
        elif router_id in _first_offline_time:
            hours_offline = (time.time() - _first_offline_time[router_id]) / 3600
            if hours_offline > LONG_OFFLINE_HOURS and router_id in _offline_alerted:
                # Send periodic reminder (every LONG_OFFLINE_HOURS)
                _send_alert(router, "long_offline",
                            f"🔴 STILL OFFLINE: {router['name']} has been down "
                            f"for {hours_offline:.1f} hours. Check physically!")
                _first_offline_time[router_id] = time.time()  # Reset timer

        update_router_status(router_id, "offline", db_path)
        return {"router_id": router_id, "name": router["name"], "status": "offline"}

    # ── Router is reachable — reset retry counter ─────────
    _retry_count[router_id] = 0

    # ── Step 2: SSH authentication test ───────────────────
    ssh_ok, ssh_msg = ssh.test_connection(router)

    if not ssh_ok:
        logger.warning(f"{router['name']}: Reachable but SSH failed — {ssh_msg}")
        update_router_status(router_id, "ssh_error", db_path)

        if previous_status != "ssh_error":
            log_event(router_id, "ssh_error",
                      f"SSH failed: {ssh_msg}", "warning", db_path)
            _send_alert(router, "ssh_error",
                        f"⚠️ SSH ERROR on {router['name']}: {ssh_msg}")

        return {"router_id": router_id, "name": router["name"], "status": "ssh_error"}

    # ── Step 3: Router is online ──────────────────────────
    update_router_status(router_id, "online", db_path)
    _offline_alerted.discard(router_id)
    _first_offline_time.pop(router_id, None)

    # ── Step 4: Came back from offline/error → check for reset ──
    came_back = previous_status in ("offline", "unknown", "ssh_error")
    if came_back:
        logger.info(f"{router['name']}: BACK ONLINE (was: {previous_status})")
        log_event(router_id, "status_online",
                  f"Back online from '{previous_status}'", "info", db_path)
        _send_alert(router, "back_online",
                    f"✅ ONLINE: {router['name']} ({router['host']}) is back!")

        # Check for factory reset
        reset_check = check_and_auto_restore(router_id, db_path)
        if reset_check["was_reset"]:
            result = reset_check.get("restore_result", {})
            restored = result.get("success", False) if result else False
            return {"router_id": router_id, "name": router["name"],
                    "status": "online", "restored": restored}

    return {"router_id": router_id, "name": router["name"], "status": "online"}


def poll_backup_all(db_path=None):
    """Fallback polling: back up all online routers."""
    routers = get_all_routers(db_path)
    results = []
    for router in routers:
        if router.get("status") == "online":
            results.append(backup_router(router["id"], "poll", db_path))
    backed_up = sum(1 for r in results if r.get("is_new"))
    logger.info(f"Poll backup: {backed_up} new from {len(results)} online routers")
    return results


def _send_alert(router, event_type, message):
    """Send webhook alert if configured."""
    try:
        url = config.get("alerts", {}).get("webhook_url", "")
        if url:
            requests.post(url, json={"text": message}, timeout=5)
    except Exception:
        pass



# ======================================================================
# MODULE: syslog_listener.py
# ======================================================================

logger = logging.getLogger("rbrcs.syslog")

# Patterns that indicate a config change (per device type)
CONFIG_CHANGE_PATTERNS = [
    # Cisco IOS
    r"SYS-5-CONFIG_I",         # Config changed from console/vty
    r"SYS-5-RESTART",          # System restart
    r"WRITE_MEM",              # Config saved
    r"CONFIG_CHANGE",
    # MikroTik
    r"system,info.*changed",
    r"config changed",
    # Generic
    r"configuration.*changed",
    r"config.*saved",
    r"startup-config.*modified",
]

COMPILED_PATTERNS = [re.compile(p, re.IGNORECASE) for p in CONFIG_CHANGE_PATTERNS]


class SyslogHandler(socketserver.BaseRequestHandler):
    """Handle incoming syslog UDP messages."""

    def handle(self):
        data = self.request[0].strip().decode("utf-8", errors="replace")
        source_ip = self.client_address[0]

        logger.debug(f"Syslog from {source_ip}: {data}")

        # Check if this matches a config change pattern
        is_config_change = any(p.search(data) for p in COMPILED_PATTERNS)

        if is_config_change:
            logger.info(f"Config change detected from {source_ip}: {data[:100]}")
            self._trigger_backup(source_ip, data)

    def _trigger_backup(self, source_ip, message):
        """Find the router by IP and trigger a backup."""
        # Import here to avoid circular imports

        routers = get_all_routers()
        for router in routers:
            if router["host"] == source_ip:
                logger.info(f"Triggering event-driven backup for {router['name']}")
                log_event(router["id"], "syslog_trigger",
                          f"Config change syslog received: {message[:200]}", "info")
                backup_router(router["id"], change_type="syslog")
                return

        logger.warning(f"Syslog from unknown IP {source_ip} — not matching any router")


class SyslogListener:
    """UDP syslog server that runs in a background thread."""

    def __init__(self, host="0.0.0.0", port=514):
        self.host = host
        self.port = port
        self.server = None
        self.thread = None

    def start(self):
        """Start the syslog listener in a background thread."""
        try:
            self.server = socketserver.UDPServer(
                (self.host, self.port), SyslogHandler
            )
            self.thread = threading.Thread(
                target=self.server.serve_forever,
                daemon=True,
                name="SyslogListener"
            )
            self.thread.start()
            logger.info(f"Syslog listener started on {self.host}:{self.port}")
        except PermissionError:
            logger.error(
                f"Cannot bind to port {self.port} — "
                "try running as root/admin or use a port > 1024"
            )
        except Exception as e:
            logger.error(f"Syslog listener failed to start: {e}")

    def stop(self):
        """Stop the syslog listener."""
        if self.server:
            self.server.shutdown()
            logger.info("Syslog listener stopped")



# ======================================================================
# MODULE: scheduler.py
# ======================================================================

logger = logging.getLogger("rbrcs.scheduler")

scheduler = BackgroundScheduler(daemon=True)


def _run_drift_check():
    """Check all routers for config drift against golden baseline."""
    routers = get_all_routers()
    for router in routers:
        if router.get("status") != "online":
            continue
        golden = get_golden_config(router["id"])
        if not golden:
            continue
        try:
            ssh = SSHManager()
            current = ssh.fetch_config(router)
            if current:
                check_drift(router["id"], current, router.get("device_type"))
        except Exception as e:
            logger.debug(f"Drift check skipped for {router['name']}: {e}")


def start_scheduler(config):
    """Start all scheduled jobs based on config."""
    health_interval = config.get("health_check", {}).get("interval_minutes", 10)
    backup_interval = config.get("health_check", {}).get("backup_interval_minutes", 30)
    cleanup_hour = config.get("retention", {}).get("cleanup_hour", 3)
    retention_config = config.get("retention", {})

    # Job 1: Health check
    scheduler.add_job(
        check_all_routers, "interval",
        minutes=health_interval,
        id="health_check", name="Router Health Check",
        replace_existing=True,
    )
    logger.info(f"Scheduled health check every {health_interval}m")

    # Job 2: Per-router backup
    routers = config.get("routers", [])
    if not routers:
        scheduler.add_job(
            poll_backup_all, "interval",
            minutes=backup_interval,
            id="poll_backup", name="Fallback Config Backup",
            replace_existing=True,
        )
    else:
        for r_cfg in routers:
            r_interval = r_cfg.get("backup_interval_minutes", backup_interval)
            scheduler.add_job(
                backup_router, "interval",
                minutes=int(r_interval),
                args=[r_cfg["id"], "poll"],
                id=f"backup_{r_cfg['id']}",
                name=f"Backup {r_cfg['name']}",
                replace_existing=True,
            )
            logger.info(f"Scheduled backup for {r_cfg['name']} every {r_interval}m")

    # Job 3: Golden config drift check — every hour
    scheduler.add_job(
        _run_drift_check, "interval",
        hours=1,
        id="drift_check", name="Golden Config Drift Check",
        replace_existing=True,
    )
    logger.info("Scheduled golden config drift check every 1h")

    # Job 4: Retention cleanup
    scheduler.add_job(
        run_retention_cleanup, "cron",
        hour=cleanup_hour, minute=0,
        args=[retention_config],
        id="retention_cleanup", name="Retention Cleanup",
        replace_existing=True,
    )
    logger.info(f"Scheduled retention cleanup daily at {cleanup_hour}:00")

    scheduler.start()
    logger.info("Scheduler started")


def stop_scheduler():
    scheduler.shutdown(wait=False)


def get_scheduled_jobs():
    jobs = []
    for job in scheduler.get_jobs():
        jobs.append({
            "id": job.id,
            "name": job.name,
            "next_run": str(job.next_run_time) if job.next_run_time else "N/A",
            "trigger": str(job.trigger),
        })
    return jobs



# ======================================================================
# MODULE: app.py
# ======================================================================

# ── Logging ────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(os.path.dirname(os.path.abspath(__file__)), "rbrcs.log"), encoding="utf-8"),
    ]
)
logger = logging.getLogger("rbrcs.app")

# ── Load Config ────────────────────────────────────────────

# CONFIG_PATH: looks next to the script file first, then current working directory
_script_dir = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_script_dir, "config.yaml")
if not os.path.exists(CONFIG_PATH):
    CONFIG_PATH = os.path.join(os.getcwd(), "config.yaml")


def load_config():
    """Load config.yaml with env var expansion. Creates default if missing."""
    if not os.path.exists(CONFIG_PATH):
        _create_default_config(CONFIG_PATH)
        print(f"[RBRCS] Created default config.yaml at: {CONFIG_PATH}")
        print("[RBRCS] Edit it to add your router details, then restart.")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        expanded = os.path.expandvars(f.read())
        return yaml.safe_load(expanded)


def _create_default_config(path):
    """Write a ready-to-use default config.yaml next to the script."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    default = """# ============================================================
# RBRCS — Router Configuration Backup & Recovery System
# Edit this file with your router details then restart.
# ============================================================

system:
  db_path: "data/rbrcs.db"
  log_level: "INFO"
  secret_key: "change-me-random-string-abc123"

syslog:
  enabled: false
  listen_host: "0.0.0.0"
  listen_port: 514

health_check:
  interval_minutes: 10
  backup_interval_minutes: 30
  ping_timeout_seconds: 5

retention:
  keep_latest: 10
  daily_keep_days: 30
  weekly_keep_weeks: 26
  monthly_keep_months: 0
  cleanup_hour: 3
  event_retention_days: 30

dashboard:
  host: "0.0.0.0"
  port: 5000
  debug: false

alerts:
  webhook_url: ""

routers:
  - id: "router-main"
    name: "Main Gateway Router"
    host: "192.168.1.1"
    port: 22
    device_type: "mikrotik_routeros"
    username: "admin"
    password: "admin"
    enable_password: ""
    restore_method: "inline"
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(default)


config = load_config()

# ── Flask App ──────────────────────────────────────────────

app = Flask(__name__, template_folder=os.path.join(_script_dir, "templates"), static_folder=os.path.join(_script_dir, "static"))
app.secret_key = config.get("system", {}).get("secret_key", "default-secret")

# Set DB path from config — resolve relative to script dir
_db_path_raw = config.get("system", {}).get("db_path", "data/rbrcs.db")
if not os.path.isabs(_db_path_raw):
    _db_path_raw = os.path.join(_script_dir, _db_path_raw)
os.makedirs(os.path.dirname(_db_path_raw), exist_ok=True)
DB_PATH = _db_path_raw  # updates the global used by get_db()

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
    
    memory_file = io.BytesIO()
    routers = get_all_routers()
    
    with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
        for router in routers:
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
    svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"></path></svg>'
    return Response(svg, mimetype="image/svg+xml")



# ── Setup / Settings API ───────────────────────────────────

@app.route("/setup")
def setup_page():
    """First-run setup wizard."""
    return render_template("setup.html", config=config)


@app.route("/api/setup/save", methods=["POST"])
def api_setup_save():
    """Save router + settings from the setup wizard or settings page."""
    global config
    data = request.json or {}

    # Build updated config dict
    new_cfg = {
        "system": {
            "db_path": "data/rbrcs.db",
            "log_level": "INFO",
            "secret_key": config.get("system", {}).get("secret_key", "rbrcs-auto-secret"),
        },
        "syslog": {"enabled": False, "listen_host": "0.0.0.0", "listen_port": 514},
        "health_check": {
            "interval_minutes": int(data.get("interval_minutes", 10)),
            "backup_interval_minutes": int(data.get("backup_interval_minutes", 30)),
            "ping_timeout_seconds": int(data.get("ping_timeout_seconds", 5)),
        },
        "retention": {
            "keep_latest": int(data.get("keep_latest", 10)),
            "daily_keep_days": 30,
            "weekly_keep_weeks": 26,
            "monthly_keep_months": 0,
            "cleanup_hour": 3,
            "event_retention_days": 30,
        },
        "dashboard": {"host": "0.0.0.0", "port": 5000, "debug": False},
        "alerts": {"webhook_url": data.get("webhook_url", "")},
        "routers": config.get("routers", []),
    }

    # Add router if provided
    router_host = data.get("router_host", "").strip()
    if router_host:
        router_id = data.get("router_id", "router-main").strip() or "router-main"
        new_router = {
            "id": router_id,
            "name": data.get("router_name", "Main Router").strip() or "Main Router",
            "host": router_host,
            "port": int(data.get("router_port", 22)),
            "device_type": data.get("device_type", "mikrotik_routeros"),
            "username": data.get("username", "admin"),
            "password": data.get("password", ""),
            "enable_password": "",
            "restore_method": "inline",
        }
        # Replace or append
        existing_ids = [r["id"] for r in new_cfg["routers"]]
        if router_id in existing_ids:
            new_cfg["routers"] = [r if r["id"] != router_id else new_router
                                   for r in new_cfg["routers"]]
        else:
            new_cfg["routers"].append(new_router)

        # Register in DB immediately
        upsert_router(new_router)
        log_event(router_id, "router_added", f"Router added via setup wizard: {new_router['name']}", "info")

    # Write config.yaml
    import yaml as _yaml
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        _yaml.dump(new_cfg, f, default_flow_style=False, allow_unicode=True)

    config = new_cfg  # update live config
    return jsonify({"success": True, "message": "Configuration saved. Ready to use."})


@app.route("/api/setup/test-connection", methods=["POST"])
def api_test_connection():
    """Test SSH connection to a router from the setup page."""
    data = request.json or {}
    test_router = {
        "id": "test",
        "name": "Test",
        "host": data.get("host", ""),
        "port": int(data.get("port", 22)),
        "username": data.get("username", "admin"),
        "password": data.get("password", ""),
        "device_type": data.get("device_type", "mikrotik_routeros"),
    }
    if not test_router["host"]:
        return jsonify({"success": False, "message": "No host provided"})

    mgr = SSHManager()
    # First TCP ping
    reachable = mgr.ping(test_router)
    if not reachable:
        return jsonify({"success": False, "message": f"Cannot reach {test_router['host']}:{test_router['port']} — check IP and that the device is on"})
    # Then SSH auth
    ok, msg = mgr.test_connection(test_router)
    return jsonify({"success": ok, "message": msg if ok else f"SSH failed: {msg}"})


# ── Golden Config API ──────────────────────────────────────

@app.route("/api/golden/<router_id>", methods=["GET"])
def api_get_golden(router_id):
    """Get golden config for a router."""
    golden = get_golden_config(router_id)
    if not golden:
        return jsonify({"exists": False, "message": "No golden config set"})
    return jsonify({
        "exists": True,
        "router_id": router_id,
        "config_size": golden["config_size"],
        "promoted_at": golden.get("promoted_at", ""),
        "promoted_by": golden.get("promoted_by", ""),
        "config_hash": golden["config_hash"],
    })


@app.route("/api/golden/<router_id>/promote", methods=["POST"])
def api_promote_golden(router_id):
    """Promote a specific backup as golden config."""
    data = request.json or {}
    config_id = data.get("config_id")

    if config_id:
        cfg = get_config_by_id(config_id)
        if not cfg:
            return jsonify({"success": False, "message": "Config not found"}), 404
        if cfg["router_id"] != router_id:
            return jsonify({"success": False, "message": "Config belongs to another router"}), 400
        set_golden_config(router_id, cfg["config_text"], "admin-dashboard")
    else:
        # Promote latest backup
        latest = get_latest_config(router_id)
        if not latest:
            return jsonify({"success": False, "message": "No backup exists"}), 404
        set_golden_config(router_id, latest["config_text"], "admin-dashboard")

    return jsonify({"success": True, "message": "Golden config promoted"})


@app.route("/api/golden/<router_id>/drift", methods=["GET"])
def api_check_drift(router_id):
    """Check current config drift against golden baseline."""
    router = get_router(router_id)
    if not router:
        return jsonify({"error": "Router not found"}), 404

    golden = get_golden_config(router_id)
    if not golden:
        return jsonify({"has_golden": False, "message": "No golden config set"})

    latest = get_latest_config(router_id)
    if not latest:
        return jsonify({"has_golden": True, "has_backup": False,
                        "message": "No backup to compare against"})

    drift = check_drift(router_id, latest["config_text"],
                        router.get("device_type"))
    drift["has_golden"] = True
    drift["has_backup"] = True
    return jsonify(drift)


@app.route("/api/golden/<router_id>", methods=["DELETE"])
def api_delete_golden(router_id):
    """Remove golden config."""
    delete_golden_config(router_id)
    return jsonify({"success": True, "message": "Golden config removed"})


@app.route("/api/bootstrap/<router_id>")
def api_bootstrap(router_id):
    """
    Return the minimum bootstrap config needed for a router
    so RBRCS can reach it after a full factory reset.
    This is the DOCUMENTATION for Problem #1 (Bootstrap Dependency)
    and Problem #9 (IP Address Loss).
    """
    router = get_router(router_id)
    if not router:
        return jsonify({"error": "Router not found"}), 404

    device_type = router.get("device_type", "cisco_ios")

    bootstrap_configs = {
        "cisco_ios": f"""! === RBRCS Bootstrap Config for {router['name']} ===
! Apply this via CONSOLE CABLE if router has been factory reset
! and RBRCS cannot reach it (no IP / no SSH)
!
enable
configure terminal
!
hostname {router['name'].replace(' ', '-')}
!
interface GigabitEthernet0/0
 ip address {router['host']} 255.255.255.0
 no shutdown
!
ip domain-name rbrcs.local
crypto key generate rsa modulus 2048
!
username {router.get('username', 'admin')} privilege 15 secret {router.get('password', 'CHANGE-ME')}
!
line vty 0 4
 login local
 transport input ssh
!
ip ssh version 2
!
end
write memory
!
! === After this, RBRCS will auto-detect and restore full config ===
""",
        "mikrotik_routeros": f"""# === RBRCS Bootstrap for {router['name']} ===
/ip address add address={router['host']}/24 interface=ether1
/ip service set ssh port=22 disabled=no
/user set admin password={router.get('password', 'CHANGE-ME')}
# After this, RBRCS will detect and restore.
""",
        "ubiquiti_edgeos": f"""# === RBRCS Bootstrap for {router['name']} ===
configure
set interfaces ethernet eth0 address {router['host']}/24
set service ssh port 22
set system login user {router.get('username', 'admin')} authentication plaintext-password {router.get('password', 'CHANGE-ME')}
commit ; save
# After this, RBRCS will detect and restore.
""",
        "generic_linux": f"""#!/bin/bash
# === RBRCS Bootstrap for {router['name']} ===
ip addr add {router['host']}/24 dev eth0
ip link set eth0 up
systemctl start sshd
# After this, RBRCS will detect and restore.
""",
    }

    return jsonify({
        "router_id": router_id,
        "device_type": device_type,
        "instructions": "Apply via CONSOLE CABLE when router is unreachable",
        "bootstrap_config": bootstrap_configs.get(device_type, "No template available"),
    })


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
        serve(app, host=host, port=port, threads=2)
