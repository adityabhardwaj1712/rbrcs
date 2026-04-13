"""
bundle.py — Updated for restructured RBRCS.
"""
import os, re

MODULES = [
    'src/utils/alerts.py',
    'src/core/database.py',
    'src/utils/ssh.py',
    'src/core/golden.py',
    'src/core/compliance.py',
    'src/core/backup.py',
    'src/core/restore.py',
    'src/core/retention.py',
    'src/core/health.py',
    'src/utils/syslog.py',
    'src/scheduler.py',
    'src/web/server.py',
    'main.py'
]

imports, body = set(), []

# Regex to detect local imports from src.*
LOCAL_IMP = re.compile(r'^(import src\.|from src\.)')

for mod in MODULES:
    if not os.path.exists(mod): continue
    with open(mod, 'r', encoding='utf-8') as f: content = f.read()
    
    clean = []
    for line in content.split('\n'):
        s = line.strip()
        if (s.startswith('import ') or s.startswith('from ')) and not LOCAL_IMP.match(s):
            # Also filter out 'from database import' which was the old style but might linger in some views if I missed any
            if not any(x in s for x in ['from database', 'from ssh_manager', 'from scheduler', 'from backup_engine']):
                imports.add(line)
        elif not LOCAL_IMP.match(s):
            clean.append(line)
            
    body.append(f"\n# {'='*40}\n# MODULE: {mod}\n# {'='*40}\n" + '\n'.join(clean))

with open('rbrcs_app_standalone.py', 'w', encoding='utf-8') as f:
    f.write('"""\nrbrcs_app_standalone.py — Restructured & Compressed\n"""\n')
    for imp in sorted(list(imports)): f.write(imp + '\n')
    f.write('\n' + '\n'.join(body) + '\n\nif __name__ == "__main__": run()')

print("Bundle created: rbrcs_app_standalone.py")
