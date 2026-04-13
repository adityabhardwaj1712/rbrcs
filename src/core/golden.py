"""
golden.py — Concise golden config (baseline) management for RBRCS.
"""
import logging, difflib, zlib, hashlib
from src.core.database import *

logger = logging.getLogger("rbrcs.golden")
CRITICAL = {"cisco_ios": ["hostname", "interface", "ip route"], "mikrotik_routeros": ["/ip address", "/ip route"]}

def set_golden_config(rid, text, user="system"):
    with db_conn() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS golden (rid TEXT PRIMARY KEY, hash TEXT, data BLOB, size INTEGER)")
        conn.execute("INSERT OR REPLACE INTO golden (rid, hash, data, size) VALUES (?, ?, ?, ?)", (rid, hashlib.md5(text.encode()).hexdigest(), zlib.compress(text.encode(), 6), len(text)))
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
