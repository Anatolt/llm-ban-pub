#!/usr/bin/env python3
"""Read LLMBAN prompt on stdin; Pi emits a final JSON line on stdout."""
import argparse
import os
import sys

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--role', choices=['planner', 'doer', 'tester'], required=True)
args = parser.parse_args()
provider = os.environ.get('LLMBAN_PI_PROVIDER', '').strip()
model = os.environ.get('LLMBAN_PI_MODEL', '').strip()
if not provider or not model:
    parser.exit(2, 'Set LLMBAN_PI_PROVIDER and LLMBAN_PI_MODEL\n')
tools = 'read,grep,find,ls' if args.role == 'planner' else 'read,bash,edit,write,grep,find,ls'
argv = ['pi', '--provider', provider, '--model', model, '--no-session', '--print',
        '--no-extensions', '--no-skills', '--no-prompt-templates', '--no-themes',
        '--tools', tools]
# Pi print mode reads the inherited stdin itself; no task text in argv/ps output.
os.environ.setdefault('PI_OFFLINE', '1')
os.environ.setdefault('PI_TELEMETRY', '0')
os.execvp(argv[0], argv)
