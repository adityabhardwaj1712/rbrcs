"""
retention.py — Concise retention logic for RBRCS.
"""
from datetime import datetime, timedelta
from src.core.database import db_conn

def run_retention_cleanup(sets=None):
    s = sets or {"latest": 10, "days": 30, "events": 30}
    with db_conn() as conn:
        for rid in [r[0] for r in conn.execute("SELECT DISTINCT id FROM routers")]:
            cfgs = conn.execute("SELECT id, timestamp FROM configs WHERE router_id=? ORDER BY timestamp DESC", (rid,)).fetchall()
            if len(cfgs) <= s["latest"]: continue
            keep = {c["id"] for c in cfgs[:s["latest"]]}
            now = datetime.utcnow()
            for c in cfgs:
                ts = datetime.fromisoformat(c["timestamp"].replace("Z", "+00:00")).replace(tzinfo=None)
                if ts > now - timedelta(days=s["days"]): keep.add(c["id"])
                if ts.day == 1: keep.add(c["id"]) # Monthly keep
            d_ids = [c["id"] for c in cfgs if c["id"] not in keep]
            if d_ids: conn.execute(f"DELETE FROM configs WHERE id IN ({','.join(['?']*len(d_ids))})", d_ids)
        
        cutoff = (datetime.utcnow() - timedelta(days=s["events"])).isoformat()
        conn.execute("DELETE FROM events WHERE timestamp < ?", (cutoff,))
        conn.execute("DELETE FROM system_logs WHERE timestamp < ?", (cutoff,))
        try: conn.execute("VACUUM")
        except: pass

def get_retention_stats():
    with db_conn() as conn:
        r = conn.execute("SELECT COUNT(*), COUNT(DISTINCT config_hash), SUM(LENGTH(config_data)), MIN(timestamp) FROM configs").fetchone()
        return {"total": r[0], "unique": r[1], "storage": r[2] or 0, "oldest": r[3]}
