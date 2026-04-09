"""
compliance.py — Configuration Security & Compliance Scanner.

Scans incoming configurations to detect severe security misconfigurations.
"""

import logging
from database import log_event

logger = logging.getLogger("rbrcs.compliance")

CISCO_RULES = [
    {
        "id": "C-01",
        "description": "Plaintext enable password used instead of secure 'secret'",
        "trigger": lambda line: line.strip().startswith("enable password") and "secret" not in line.strip(),
        "severity": "error"
    },
    {
        "id": "C-02",
        "description": "Weak Type 7 password encryption detected",
        "trigger": lambda line: "password 7 " in line.strip(),
        "severity": "warning"
    },
    {
        "id": "C-03",
        "description": "Password encryption service is disabled",
        "trigger": lambda line: line.strip() == "no service password-encryption",
        "severity": "warning"
    }
]

MIKROTIK_RULES = [
    {
        "id": "M-01",
        "description": "Default admin empty password detected (or misconfigured user)",
        "trigger": lambda line: "/user add name=admin" in line and "password=" not in line,
        "severity": "error"
    }
]


def run_compliance_check(router_id, device_type, config_text, db_path=None):
    """
    Run intelligence rules upon a new configuration text format.
    """
    if not config_text:
        return

    rules = []
    if device_type == "cisco_ios":
        rules = CISCO_RULES
    elif device_type == "mikrotik_routeros":
        rules = MIKROTIK_RULES

    if not rules:
        return

    lines = config_text.splitlines()
    triggered_rules = set()

    for line in lines:
        for rule in rules:
            if rule["id"] not in triggered_rules:
                if rule["trigger"](line):
                    triggered_rules.add(rule["id"])
                    
                    msg = f"Security Compliance Risk [{rule['id']}]: {rule['description']} (Line detected: '{line.strip()[:40]}...')"
                    logger.warning(f"Compliance failed on Router {router_id}: {msg}")
                    log_event(router_id, "security_compliance", msg, rule["severity"], db_path)

    if not triggered_rules:
        logger.debug(f"Router {router_id} passed compliance checks.")

