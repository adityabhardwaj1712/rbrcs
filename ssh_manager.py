"""
ssh_manager.py — SSH connection manager for multiple router types.

Supports:
  - Cisco IOS
  - MikroTik RouterOS
  - Ubiquiti EdgeOS
  - Generic Linux
"""

import paramiko
import socket
import time
import logging

logger = logging.getLogger("rbrcs.ssh")


# Default commands per device type
DEFAULT_COMMANDS = {
    "cisco_ios": ["terminal length 0", "show running-config"],
    "mikrotik_routeros": ["/export"],
    "ubiquiti_edgeos": ["show configuration"],
    "generic_linux": ["cat /etc/network/interfaces"],
}

# Config push commands per device type
RESTORE_PREAMBLE = {
    "cisco_ios": ["configure terminal"],
    "mikrotik_routeros": [],
    "ubiquiti_edgeos": ["configure"],
    "generic_linux": [],
}

RESTORE_POSTAMBLE = {
    "cisco_ios": ["end", "write memory"],
    "mikrotik_routeros": [],
    "ubiquiti_edgeos": ["commit", "save", "exit"],
    "generic_linux": [],
}


class SSHManager:
    """Manages SSH connections to routers with pooling."""
    _pool = {}  # router_id -> (client, timestamp)

    def __init__(self, timeout=30):
        self.timeout = timeout

    def _connect(self, router):
        """Create an SSH connection to a router or return a cached one."""
        router_id = router.get("id")
        
        if router_id in self._pool:
            client, last_used = self._pool[router_id]
            if time.time() - last_used < 300:  # 5 minutes TTL
                try:
                    transport = client.get_transport()
                    if transport and transport.is_active():
                        self._pool[router_id] = (client, time.time())
                        return client
                except Exception:
                    pass
            # Stale or dead connection, clean up
            try:
                client.close()
            except Exception:
                pass
            del self._pool[router_id]

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs = {
            "hostname": router["host"],
            "port": router.get("port", 22),
            "username": router.get("username", "admin"),
            "timeout": self.timeout,
            "look_for_keys": False,
            "allow_agent": False,
        }

        # Use SSH key if specified, otherwise password
        ssh_key_path = router.get("ssh_key_path", "")
        if ssh_key_path:
            connect_kwargs["key_filename"] = ssh_key_path
        else:
            connect_kwargs["password"] = router.get("password", "")

        client.connect(**connect_kwargs)
        
        if router_id:
            self._pool[router_id] = (client, time.time())
            
        return client

    def test_connection(self, router):
        """Test if a router is reachable via SSH. Returns (success, message)."""
        try:
            client = self._connect(router)
            # We don't close it, leave it in pool
            return True, "Connection successful"
        except paramiko.AuthenticationException:
            return False, "Authentication failed"
        except paramiko.SSHException as e:
            return False, f"SSH error: {e}"
        except socket.timeout:
            return False, "Connection timed out"
        except socket.error as e:
            return False, f"Network error: {e}"
        except Exception as e:
            return False, f"Unknown error: {e}"

    def ping(self, router):
        """Quick TCP check if SSH port is open."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            result = sock.connect_ex((router["host"], router.get("port", 22)))
            sock.close()
            return result == 0
        except Exception:
            return False

    def fetch_config(self, router, custom_commands=None):
        """
        SSH into a router and fetch its running configuration.
        Returns the config text as a string.
        """
        device_type = router.get("device_type", "cisco_ios")
        commands = custom_commands or router.get(
            "backup_commands",
            DEFAULT_COMMANDS.get(device_type, ["show running-config"])
        )

        client = None
        try:
            client = self._connect(router)

            if device_type == "cisco_ios":
                return self._fetch_cisco(client, router, commands)
            else:
                return self._fetch_generic(client, commands)

        finally:
            # Pooled connection will remain open
            pass

    def _fetch_cisco(self, client, router, commands):
        """Fetch config from Cisco IOS using an interactive shell (handles enable mode)."""
        shell = client.invoke_shell()
        time.sleep(1)
        shell.recv(65535)  # Clear banner

        # Enter enable mode if needed
        enable_pwd = router.get("enable_password", "")
        if enable_pwd:
            shell.send("enable\n")
            time.sleep(1)
            shell.recv(65535)
            shell.send(enable_pwd + "\n")
            time.sleep(1)
            shell.recv(65535)

        output_parts = []
        for cmd in commands:
            shell.send(cmd + "\n")
            time.sleep(2)
            while shell.recv_ready():
                chunk = shell.recv(65535).decode("utf-8", errors="replace")
                output_parts.append(chunk)
                time.sleep(0.5)

        shell.close()
        full_output = "".join(output_parts)

        # Clean up Cisco output (remove command echo, prompts)
        lines = full_output.split("\n")
        clean_lines = []
        capture = False
        for line in lines:
            stripped = line.strip()
            if "show running-config" in stripped:
                capture = True
                continue
            if capture:
                # Stop at the next prompt (hostname#)
                if stripped.endswith("#") and len(stripped) < 60:
                    break
                clean_lines.append(line.rstrip())

        return "\n".join(clean_lines)

    def _fetch_generic(self, client, commands):
        """Fetch config using exec commands (MikroTik, Ubiquiti, Linux)."""
        output_parts = []
        for cmd in commands:
            stdin, stdout, stderr = client.exec_command(cmd, timeout=30)
            output = stdout.read().decode("utf-8", errors="replace")
            output_parts.append(output)
        return "\n".join(output_parts)

    def push_config(self, router, config_text):
        """
        Push a configuration to a router.
        Returns (success, message).
        """
        device_type = router.get("device_type", "cisco_ios")
        client = None

        try:
            client = self._connect(router)

            if device_type == "cisco_ios":
                return self._push_cisco(client, router, config_text)
            elif device_type == "mikrotik_routeros":
                return self._push_mikrotik(client, config_text)
            else:
                return self._push_generic(client, config_text)

        except Exception as e:
            return False, f"Restore failed: {e}"
        finally:
            pass

    def execute_commands(self, router, commands_text):
        """
        Execute arbitrary commands/configurations and RETURN the complete terminal output.
        Designed for dynamic UI ad-hoc configuration execution.
        """
        device_type = router.get("device_type", "cisco_ios")
        client = None

        try:
            client = self._connect(router)
            
            if device_type == "cisco_ios":
                shell = client.invoke_shell()
                time.sleep(1)
                shell.recv(65535)

                enable_pwd = router.get("enable_password", "")
                if enable_pwd:
                    shell.send("enable\n")
                    time.sleep(1)
                    shell.recv(65535)
                    shell.send(enable_pwd + "\n")
                    time.sleep(1)
                    shell.recv(65535)

                shell.send("configure terminal\n")
                time.sleep(1)
                
                output_parts = []
                for line in commands_text.split("\n"):
                    line = line.strip()
                    if not line: continue
                    shell.send(line + "\n")
                    time.sleep(0.5)
                    while shell.recv_ready():
                        output_parts.append(shell.recv(65535).decode("utf-8", errors="replace"))

                shell.send("end\n")
                time.sleep(1)
                while shell.recv_ready():
                    output_parts.append(shell.recv(65535).decode("utf-8", errors="replace"))
                
                shell.close()
                return True, "".join(output_parts)
                
            else:
                output_parts = []
                for line in commands_text.split("\n"):
                    if not line.strip(): continue
                    stdin, stdout, stderr = client.exec_command(line.strip(), timeout=30)
                    output_parts.append(stdout.read().decode("utf-8", errors="replace"))
                    err = stderr.read().decode("utf-8", errors="replace")
                    if err:
                        output_parts.append(err)
                return True, "\n".join(output_parts)

        except Exception as e:
            return False, f"Execution failed: {str(e)}"
        finally:
            pass

    def _push_cisco(self, client, router, config_text):
        """Push config to Cisco IOS line-by-line."""
        shell = client.invoke_shell()
        time.sleep(1)
        shell.recv(65535)

        # Enable mode
        enable_pwd = router.get("enable_password", "")
        if enable_pwd:
            shell.send("enable\n")
            time.sleep(1)
            shell.recv(65535)
            shell.send(enable_pwd + "\n")
            time.sleep(1)
            shell.recv(65535)

        # Enter config mode
        shell.send("configure terminal\n")
        time.sleep(1)
        shell.recv(65535)

        # Send config line by line
        for line in config_text.split("\n"):
            line = line.strip()
            if not line or line.startswith("!") or line.startswith("Building"):
                continue
            if line.startswith("Current configuration"):
                continue
            if line == "end":
                continue
            shell.send(line + "\n")
            time.sleep(0.1)

        time.sleep(2)
        shell.recv(65535)

        # Exit and save
        shell.send("end\n")
        time.sleep(1)
        shell.send("write memory\n")
        time.sleep(3)
        output = shell.recv(65535).decode("utf-8", errors="replace")
        shell.close()

        if "OK" in output or "bytes copied" in output or "[OK]" in output:
            return True, "Configuration restored and saved"
        return True, "Configuration pushed (verify manually)"

    def _push_mikrotik(self, client, config_text):
        """Push config to MikroTik via import."""
        # Upload config as a file, then import
        sftp = client.open_sftp()
        sftp.open("/tmp/restore.rsc", "w").write(config_text)
        sftp.close()

        stdin, stdout, stderr = client.exec_command("/import file=/tmp/restore.rsc")
        output = stdout.read().decode("utf-8", errors="replace")
        errors = stderr.read().decode("utf-8", errors="replace")

        if errors:
            return False, f"Import errors: {errors}"
        return True, "Configuration imported"

    def _push_generic(self, client, config_text):
        """Generic restore — write to file."""
        sftp = client.open_sftp()
        with sftp.open("/tmp/restored_config.txt", "w") as f:
            f.write(config_text)
        sftp.close()
        return True, "Config saved to /tmp/restored_config.txt on device"
