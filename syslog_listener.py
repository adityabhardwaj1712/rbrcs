"""
syslog_listener.py — UDP syslog listener for event-driven backups.

Listens for syslog messages from routers and triggers immediate backup
when a configuration change event is detected.
"""

import socketserver
import threading
import re
import logging
from database import get_all_routers, log_event

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
        from backup_engine import backup_router

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
