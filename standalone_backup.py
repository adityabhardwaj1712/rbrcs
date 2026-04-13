import paramiko
import yaml
import os
import time
from datetime import datetime

# Default commands per device type
DEFAULT_COMMANDS = {
    "cisco_ios": ["terminal length 0", "show running-config"],
    "mikrotik_routeros": ["/export"],
    "ubiquiti_edgeos": ["show configuration"],
    "generic_linux": ["cat /etc/network/interfaces"],
}

def load_config(config_path="config.yaml"):
    """Load the router configuration from YAML file."""
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(os.path.expandvars(f.read()))

def fetch_config(router):
    """Connect to the router via SSH and fetch its configuration."""
    device_type = router.get("device_type", "cisco_ios")
    commands = router.get("backup_commands", DEFAULT_COMMANDS.get(device_type, ["show running-config"]))
    
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    connect_kwargs = {
        "hostname": router["host"],
        "port": router.get("port", 22),
        "username": router.get("username", "admin"),
        "timeout": 30,
        "look_for_keys": False,
        "allow_agent": False,
    }
    
    ssh_key_path = router.get("ssh_key_path", "")
    if ssh_key_path:
        connect_kwargs["key_filename"] = ssh_key_path
    else:
        connect_kwargs["password"] = router.get("password", "")

    try:
        print(f"Connecting to {router.get('name', router['host'])} ({router['host']})...")
        client.connect(**connect_kwargs)
        
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
            
            # Clean up Cisco output
            lines = full_output.split("\n")
            clean_lines = []
            capture = False
            for line in lines:
                stripped = line.strip()
                if "show running-config" in stripped:
                    capture = True
                    continue
                if capture:
                    if stripped.endswith("#") and len(stripped) < 60:
                        break
                    clean_lines.append(line.rstrip())
            return "\n".join(clean_lines)
            
        else:
            output_parts = []
            for cmd in commands:
                stdin, stdout, stderr = client.exec_command(cmd, timeout=30)
                output_parts.append(stdout.read().decode("utf-8", errors="replace"))
            return "\n".join(output_parts)

    except Exception as e:
        print(f"Failed to fetch config for {router.get('name', router['host'])}: {e}")
        return None
    finally:
        client.close()

def main():
    if not os.path.exists("config.yaml"):
        print("Error: config.yaml not found in the current directory.")
        return
        
    config = load_config("config.yaml")
    routers = config.get("routers", [])
    if not routers:
        print("No routers found in config.yaml.")
        return
        
    backup_dir = "backups"
    if not os.path.exists(backup_dir):
        os.makedirs(backup_dir)
        
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    print(f"Starting backup for {len(routers)} routers...")
    
    for router in routers:
        router_id = router.get("id", router["host"].replace(".", "_"))
        router_dir = os.path.join(backup_dir, router_id)
        if not os.path.exists(router_dir):
            os.makedirs(router_dir)
            
        config_text = fetch_config(router)
        if config_text:
            filename = os.path.join(router_dir, f"backup_{timestamp}.txt")
            with open(filename, "w", encoding="utf-8") as f:
                f.write(config_text)
            print(f"[SUCCESS] Backed up {router.get('name', router_id)} to {filename}")
        else:
            print(f"[FAILED] Could not backup {router.get('name', router_id)}")
            
    print("-" * 40)
    print("Backup process completed.")

if __name__ == "__main__":
    main()
