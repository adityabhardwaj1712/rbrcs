"""
ssh.py — Concise SSH manager for RBRCS.
"""
import paramiko, socket, time, logging

logger = logging.getLogger("rbrcs.ssh")
CMDS = {
    "cisco_ios": {"get": ["terminal length 0", "show run"], "pre": ["conf t"], "post": ["end", "wr mem"]},
    "mikrotik_routeros": {"get": ["/export"], "pre": [], "post": []},
    "ubiquiti_edgeos": {"get": ["show configuration"], "pre": ["configure"], "post": ["commit", "save", "exit"]},
}

class SSHManager:
    _pool = {}

    def __init__(self, timeout=30): self.timeout = timeout

    def _connect(self, r):
        rid = r.get("id")
        if rid in self._pool:
            c, t = self._pool[rid]
            if time.time() - t < 300 and c.get_transport() and c.get_transport().is_active(): return c
            try: c.close()
            except: pass
        
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        args = {"hostname": r["host"], "port": r.get("port", 22), "username": r.get("username", "admin"), "timeout": self.timeout, "look_for_keys": False, "allow_agent": False}
        if r.get("ssh_key_path"): args["key_filename"] = r["ssh_key_path"]
        else: args["password"] = r.get("password", "")
        
        c.connect(**args)
        if rid: self._pool[rid] = (c, time.time())
        return c

    def test_connection(self, r):
        try: self._connect(r); return True, "Connected"
        except Exception as e: return False, str(e)

    def ping(self, r):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5); res = s.connect_ex((r["host"], r.get("port", 22))); s.close()
            return res == 0
        except: return False

    def fetch_config(self, r):
        dtype = r.get("device_type", "cisco_ios")
        cmds = CMDS.get(dtype, CMDS["cisco_ios"])["get"]
        c = self._connect(r)
        if dtype == "cisco_ios":
            sh = c.invoke_shell(); time.sleep(1); sh.recv(65535)
            if r.get("enable_password"):
                sh.send("enable\n"); time.sleep(1); sh.recv(65535); sh.send(r["enable_password"]+"\n"); time.sleep(1); sh.recv(65535)
            res = ""
            for cmd in cmds:
                sh.send(cmd+"\n"); time.sleep(2)
                while sh.recv_ready(): res += sh.recv(65535).decode(errors="replace")
            sh.close()
            return res
        return "\n".join([c.exec_command(cmd)[1].read().decode(errors="replace") for cmd in cmds])

    def execute_commands(self, r, text):
        dtype = r.get("device_type", "cisco_ios")
        c = self._connect(r)
        if dtype == "cisco_ios":
            sh = c.invoke_shell(); time.sleep(1); sh.recv(65535)
            for line in (["enable", r["enable_password"]] if r.get("enable_password") else []) + ["conf t"] + text.split("\n") + ["end"]:
                if line: sh.send(line.strip()+"\n"); time.sleep(0.5)
            res = ""
            while sh.recv_ready() or time.sleep(1) or sh.recv_ready(): res += sh.recv(65535).decode(errors="replace")
            sh.close(); return True, res
        res = []
        for line in text.split("\n"):
            if not line.strip(): continue
            _, out, err = c.exec_command(line.strip())
            res.append(out.read().decode(errors="replace") + err.read().decode(errors="replace"))
        return True, "\n".join(res)

    def push_config(self, r, text):
        dtype = r.get("device_type", "cisco_ios")
        if dtype == "mikrotik_routeros":
            c = self._connect(r); sftp = c.open_sftp(); f = sftp.open("/restore.rsc", "w"); f.write(text); f.close(); sftp.close()
            _, out, err = c.exec_command("/import file=restore.rsc"); return not err.read(), "Imported"
        return self.execute_commands(r, text)
