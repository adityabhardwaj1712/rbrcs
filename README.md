# RBRCS — Router Configuration Backup & Recovery System

A lightweight, professional-grade system for automatic router configuration backup and recovery.

## Features

- **Event-driven backups** via syslog listener (real-time config change detection)
- **Fallback polling** for routers without syslog support
- **SQLite storage** with zlib compression and hash-based deduplication
- **Smart retention** — keeps recent configs in detail, older ones as snapshots
- **Auto-restore** — detects factory resets and pushes last known good config
- **Web dashboard** — view router status, backup history, config diffs
- **Multi-vendor support** — Cisco IOS, MikroTik, Ubiquiti, generic Linux

## Quick Start

### 1. Install Python Dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure Your Routers

Edit `config.yaml` and add your router details:

```yaml
routers:
  - id: "my-router"
    name: "Office Router"
    host: "192.168.1.1"
    port: 22
    device_type: "cisco_ios"  # or: mikrotik_routeros, ubiquiti_edgeos, generic_linux
    username: "admin"
    password: "your-password"
```

### 3. Run the System

```bash
python app.py
```

Open your browser to **http://localhost:5000**

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  RBRCS System                                           │
│                                                          │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │ Syslog       │  │ Health       │  │ Scheduler    │  │
│  │ Listener     │  │ Checker      │  │ (APScheduler)│  │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘  │
│         │                 │                  │          │
│         ▼                 ▼                  ▼          │
│  ┌─────────────────────────────────────────────────┐   │
│  │         Backup Engine / Restore Engine           │   │
│  └───────────────────────┬─────────────────────────┘   │
│                          │                              │
│         ┌────────────────┼────────────────┐            │
│         ▼                ▼                ▼            │
│  ┌────────────┐  ┌────────────┐  ┌────────────────┐   │
│  │ SSH Manager│  │  SQLite DB │  │ Flask Dashboard│   │
│  └────────────┘  └────────────┘  └────────────────┘   │
└─────────────────────────────────────────────────────────┘
```

## File Structure

| File | Purpose |
|------|---------|
| `app.py` | Flask web dashboard & main entry point |
| `config.yaml` | Router definitions & system settings |
| `database.py` | SQLite schema, storage, queries |
| `ssh_manager.py` | SSH connections (multi-vendor) |
| `backup_engine.py` | Config fetch → hash → dedup → store |
| `restore_engine.py` | Auto-restore & factory reset detection |
| `syslog_listener.py` | UDP syslog listener (event-driven) |
| `health_checker.py` | Periodic status checks & polling |
| `retention.py` | Smart retention policy |
| `scheduler.py` | APScheduler job management |

## Resource Usage

| Metric | Value |
|--------|-------|
| RAM | ~15 MB |
| CPU (idle) | 0% |
| CPU (checking) | <1% for 2-3 sec |
| Storage per config | ~2-5 KB (compressed) |
| Database overhead | ~500 KB |

## Supported Router Types

| Type | Backup Command | Restore Method |
|------|---------------|----------------|
| Cisco IOS | `show running-config` | Line-by-line via SSH |
| MikroTik RouterOS | `/export` | File import via SCP |
| Ubiquiti EdgeOS | `show configuration` | Configure mode |
| Generic Linux | `cat /etc/network/interfaces` | SCP file upload |

## Enabling Syslog (Optional)

For real-time config change detection, configure your router to send syslog:

### Cisco IOS
```
logging host <RBRCS_SERVER_IP>
logging trap informational
```

### MikroTik
```
/system logging action set remote target=remote remote=<RBRCS_SERVER_IP>
/system logging add action=remote topics=system,info
```

Then enable syslog in `config.yaml`:
```yaml
syslog:
  enabled: true
  listen_port: 514
```

## License

MIT
