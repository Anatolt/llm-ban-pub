#!/usr/bin/env python3
"""Generate private instance configuration once; never overwrite existing files."""
import argparse
import json
import os
from pathlib import Path
import secrets

ROOT = Path(__file__).resolve().parents[1]
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--native', action='store_true', help='Use local absolute paths instead of container paths')
args = p.parse_args()
env = ROOT / '.env'
if not env.exists():
    text = (ROOT / '.env.example').read_text()
    text = text.replace("LLMBAN_PASSWORD=''\n", 'LLMBAN_PASSWORD=' + secrets.token_urlsafe(32) + '\n')
    if hasattr(os, 'getuid') and os.getuid() != 0:
        text = text.replace('LLMBAN_UID=1000', f'LLMBAN_UID={os.getuid()}')
        text = text.replace('LLMBAN_GID=1000', f'LLMBAN_GID={os.getgid()}')
    with open(env, 'x', opener=lambda path, flags: os.open(path, flags, 0o600)) as f:
        f.write(text)
    print('Created .env with fresh credentials. View/edit it locally; do not share it.')
else:
    print('Preserved existing .env')
config = ROOT / 'config' / 'agents.json'
if not config.exists():
    data = json.loads((ROOT / 'config' / 'agents.example.json').read_text())
    if args.native:
        data['projects']['demo'] = str(ROOT / 'workspaces' / 'demo')
        for command in data['commands'].values():
            command[1] = str(ROOT / 'bin' / 'pi_adapter.py')
    config.write_text(json.dumps(data, indent=2) + '\n')
    print('Created config/agents.json')
(ROOT / 'workspaces' / 'demo').mkdir(parents=True, exist_ok=True)
