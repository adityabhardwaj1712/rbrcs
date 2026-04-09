"""
retention.py — Smart retention + event log cleanup (Ultra-Lightweight).

STRATEGY (keeps DB small forever):
  Configs:
    - Keep last N versions per router (default: 10)
    - Keep 1/day for last 30 days
    - Keep 1/week for last 26 weeks
    - Keep 1/month beyond that
  Events:
    - Auto-delete events older than configured days (default: 30)
    - Runs with retention cleanup (daily at 3 AM)
  DB Maintenance:
    - VACUUM after cleanup to reclaim disk space
"""

import logging
from datetime import datetime, timedelta
from database import get_db

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
