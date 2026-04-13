import logging, yaml, requests, os

def send_alert(router, etype, msg):
    try:
        with open("config.yaml", "r") as f: url = yaml.safe_load(os.path.expandvars(f.read())).get("alerts", {}).get("webhook_url")
        if url: requests.post(url, json={"text": msg}, timeout=5)
    except: pass
