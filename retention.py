"""
retention.py — Smart retention policy for config backups.

Strategy:
  - Keep last N versions (always)
  - Keep 1 per day for the last 30 days
  - Keep 1 per week for the last 6 months
  - Keep 1 per month beyond that
  - Delete duplicates (same hash in sequence)
"""

import logging
from datetime import datetime, timedelta
from database import get_db

logger = logging.getLogger("rbrcs.retention")


def run_retention_cleanup(config=None, db_path=None):
    """
    Run the smart retention policy on all routers.
    """
    settings = config or {
        "keep_latest": 10,
        "daily_keep_days": 30,
        "weekly_keep_weeks": 26,
    }

    conn = get_db(db_path)

    # Get all router IDs
    routers = conn.execute("SELECT DISTINCT id FROM routers").fetchall()
    total_deleted = 0

    for router_row in routers:
        router_id = router_row["id"]
        deleted = _cleanup_router(conn, router_id, settings)
        total_deleted += deleted

    conn.commit()
    conn.close()

    if total_deleted > 0:
        logger.info(f"Retention cleanup: deleted {total_deleted} old config(s)")
    else:
        logger.debug("Retention cleanup: nothing to delete")

    return total_deleted


def _cleanup_router(conn, router_id, settings):
    """Apply retention policy to a single router's backups."""
    keep_latest = settings.get("keep_latest", 10)
    daily_days = settings.get("daily_keep_days", 30)
    weekly_weeks = settings.get("weekly_keep_weeks", 26)

    # Get all configs for this router, ordered by timestamp
    all_configs = conn.execute("""
        SELECT id, timestamp, config_hash FROM configs
        WHERE router_id = ?
        ORDER BY timestamp DESC
    """, (router_id,)).fetchall()

    if len(all_configs) <= keep_latest:
        return 0  # Not enough to prune

    # IDs to keep
    keep_ids = set()

    # Rule 1: Always keep the latest N
    for cfg in all_configs[:keep_latest]:
        keep_ids.add(cfg["id"])

    # Rule 2: Keep 1 per day for last N days
    now = datetime.utcnow()
    daily_cutoff = now - timedelta(days=daily_days)
    kept_days = set()

    for cfg in all_configs:
        ts = datetime.fromisoformat(cfg["timestamp"].replace("Z", "+00:00"))
        if ts >= daily_cutoff:
            day_key = ts.strftime("%Y-%m-%d")
            if day_key not in kept_days:
                keep_ids.add(cfg["id"])
                kept_days.add(day_key)

    # Rule 3: Keep 1 per week for last N weeks
    weekly_cutoff = now - timedelta(weeks=weekly_weeks)
    kept_weeks = set()

    for cfg in all_configs:
        ts = datetime.fromisoformat(cfg["timestamp"].replace("Z", "+00:00"))
        if ts >= weekly_cutoff:
            week_key = ts.strftime("%Y-W%W")
            if week_key not in kept_weeks:
                keep_ids.add(cfg["id"])
                kept_weeks.add(week_key)

    # Rule 4: Keep 1 per month (forever)
    kept_months = set()
    for cfg in all_configs:
        ts = datetime.fromisoformat(cfg["timestamp"].replace("Z", "+00:00"))
        month_key = ts.strftime("%Y-%m")
        if month_key not in kept_months:
            keep_ids.add(cfg["id"])
            kept_months.add(month_key)

    # Delete configs not in keep_ids
    all_ids = {cfg["id"] for cfg in all_configs}
    delete_ids = all_ids - keep_ids

    if delete_ids:
        placeholders = ",".join("?" * len(delete_ids))
        conn.execute(
            f"DELETE FROM configs WHERE id IN ({placeholders})",
            list(delete_ids)
        )

    return len(delete_ids)


def get_retention_stats(db_path=None):
    """Get retention statistics for the dashboard."""
    conn = get_db(db_path)

    total = conn.execute("SELECT COUNT(*) as cnt FROM configs").fetchone()["cnt"]
    storage = conn.execute(
        "SELECT COALESCE(SUM(LENGTH(config_data)), 0) as total FROM configs"
    ).fetchone()["total"]

    # Oldest config
    oldest = conn.execute(
        "SELECT MIN(timestamp) as oldest FROM configs"
    ).fetchone()["oldest"]

    # Unique hashes (distinct configs)
    unique = conn.execute(
        "SELECT COUNT(DISTINCT config_hash) as cnt FROM configs"
    ).fetchone()["cnt"]

    conn.close()

    return {
        "total_backups": total,
        "unique_configs": unique,
        "duplicates_avoided": total - unique,
        "total_storage_bytes": storage,
        "oldest_backup": oldest,
    }
