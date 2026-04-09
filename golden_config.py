"""
golden_config.py — Golden (approved/baseline) configuration management.

SOLVES:
  - Problem #3  (Human Error)     → drift detection against approved config
  - Problem #4  (Unauthorized Access) → any change from golden = alert
  - Problem #5  (Config Overwrite) → golden is never overwritten, always the restore source
  - Problem #6  (Partial Config Loss) → section-level comparison against golden
  - Problem #11 (Multi-Router Inconsistency) → each router has its own golden baseline

HOW IT WORKS:
  1. Admin promotes a backup to "golden" via dashboard or API
  2. Every new backup is compared against the golden config
  3. Missing critical sections or unexpected changes trigger alerts
  4. Auto-restore uses golden config first (if set), then last-good-backup
"""

import os
import logging
import difflib
from database import get_db, compress_config, decompress_config, compute_hash, log_event

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
