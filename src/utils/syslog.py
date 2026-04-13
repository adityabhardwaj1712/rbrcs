"""
syslog.py — Concise syslog listener for RBRCS.
"""
import socketserver, threading, re, logging
from src.core.database import get_all_routers, log_event

logger = logging.getLogger("rbrcs.syslog")
PATTERNS = [re.compile(p, re.I) for p in [r"SYS-5-CONFIG_I", r"WRITE_MEM", r"config changed", r"system,info.*changed"]]

class SyslogHandler(socketserver.BaseRequestHandler):
    def handle(self):
        data = self.request[0].decode(errors="replace"); ip = self.client_address[0]
        if any(p.search(data) for p in PATTERNS):
            from src.core.backup import backup_router
            for r in get_all_routers():
                if r["host"] == ip:
                    log_event(r["id"], "syslog_trigger", f"Change detected: {data[:100]}")
                    backup_router(r["id"], "syslog")

class SyslogListener:
    def __init__(self, host="0.0.0.0", port=514): self.host, self.port = host, port
    def start(self):
        try:
            self.server = socketserver.UDPServer((self.host, self.port), SyslogHandler)
            threading.Thread(target=self.server.serve_forever, daemon=True).start()
            logger.info(f"Syslog live on {self.port}")
        except Exception as e: logger.error(f"Syslog fail: {e}")
