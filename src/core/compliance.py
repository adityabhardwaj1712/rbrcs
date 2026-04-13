"""
compliance.py — Concise security scanner for RBRCS.
"""
import logging
from src.core.database import log_event

RULES = {
    "cisco_ios": [
        ("C1", "error", "Plaintext enable", lambda l: l.startswith("enable password")),
        ("C2", "warning", "Weak Type 7", lambda l: "password 7 " in l),
        ("C3", "error", "Telnet input", lambda l: "transport input telnet" in l),
        ("C4", "warning", "HTTP server", lambda l: "ip http server" in l),
    ],
    "mikrotik_routeros": [
        ("M1", "error", "Default admin", lambda l: "name=admin" in l and "password=" not in l),
    ]
}

def generate_security_report(dtype, text):
    if not text: return {"score": 0, "grade": "N/A", "failed": []}
    failed = []
    for rid, sev, desc, trig in RULES.get(dtype, []):
        if any(trig(l.strip()) for l in text.split("\n")):
            failed.append({"id": rid, "severity": sev, "description": desc})
    
    score = max(0, 100 - sum(20 if f["severity"] == "error" else 10 for f in failed))
    grade = "A" if score >= 90 else "B" if score >= 80 else "C" if score >= 70 else "D" if score >= 60 else "F"
    return {"score": score, "grade": grade, "failed": failed}

def run_compliance_check(rid, dtype, text):
    rep = generate_security_report(dtype, text)
    for f in rep["failed"]: log_event(rid, "compliance_violation", f"{f['id']}: {f['description']}", f["severity"])
