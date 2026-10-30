#!/usr/bin/env python3
"""Guard against accidentally committing runtime files and common credentials."""
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
files = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
failures = []
patterns = [r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
            r'\b(?:sk-proj-|sk-ant-)[A-Za-z0-9_-]{20,}',
            r'\bgh[pousr]_[A-Za-z0-9]{30,}',
            r'\b[0-9]{8,12}:[A-Za-z0-9_-]{30,}\b']
for file in filter(None, files):
    p = Path(file)
    if (p.parts[0] in {'data', 'backups', 'workspaces'} or
            (p.name.startswith('.env') and p.name != '.env.example') or
            file == 'config/agents.json' or '.sqlite' in p.name):
        failures.append(file + ': runtime file tracked')
    path = ROOT / p
    if path.is_file():
        text = path.read_text(errors='replace')
        if any(re.search(pattern, text) for pattern in patterns):
            failures.append(file + ': possible credential (value redacted)')
if failures:
    raise SystemExit('\n'.join(failures))
print('Tracked files pass publication hygiene checks (not a full security audit).')
