"""
compliance.py — Configuration Security & Compliance Scanner (Enhanced).

SOLVES:
  #4  Unauthorized Access    → detects weak passwords, open access
  #6  Partial Config Loss    → checks for missing critical services
  #8  Corrupted Config       → detects unusual patterns

Lightweight: pure string matching, zero external dependencies.
"""

import logging
from database import log_event

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


def generate_security_report(device_type, config_text):
    """
    Evaluates the configuration and returns a structured JSON payload
    with score, grade, failed rules, and passed rules.
    """
    if not config_text:
        return {"score": 0, "grade": "UNKNOWN", "failed": [], "passed": []}

    rules = []
    if device_type == "cisco_ios":
        rules = CISCO_RULES
    elif device_type == "mikrotik_routeros":
        rules = MIKROTIK_RULES

    lines = config_text.splitlines()
    triggered_ids = set()
    failed = []

    for line in lines:
        for rule in rules:
            if rule["id"] not in triggered_ids and rule["trigger"](line):
                triggered_ids.add(rule["id"])
                failed.append({
                    "id": rule["id"],
                    "severity": rule["severity"],
                    "description": rule["description"]
                })

    # Section presence checks as rules
    sections = REQUIRED_SECTIONS.get(device_type, {})
    config_lower = config_text.lower()
    for keyword, description in sections.items():
        if keyword.lower() not in config_lower:
            rule_id = f"SEC-{len(triggered_ids)+1}"
            triggered_ids.add(rule_id)
            failed.append({
                "id": rule_id,
                "severity": "warning",
                "description": f"Missing: {description}"
            })

    passed = [r for r in rules if r["id"] not in triggered_ids]

    total_checks = len(rules) + len(sections)
    if total_checks == 0:
        return {"score": 100, "grade": "N/A", "failed": [], "passed": []}
    
    # Calculate score
    # An error drops score significantly
    error_count = sum(1 for f in failed if f["severity"] == "error")
    warning_count = sum(1 for f in failed if f["severity"] == "warning")
    
    score = 100 - (error_count * 20) - (warning_count * 10)
    score = max(0, score)
    
    if score >= 90:
        grade = "A"
    elif score >= 80:
        grade = "B"
    elif score >= 70:
        grade = "C"
    elif score >= 60:
        grade = "D"
    else:
        grade = "F"

    return {
        "score": score,
        "grade": grade,
        "failed": failed,
        "passed": passed
    }
