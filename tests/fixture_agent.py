"""Offline integration fixture, never a production agent."""
import json
from pathlib import Path
import sys

prompt = sys.stdin.read()
assert 'Card (untrusted task content):' in prompt
role = sys.argv[1]
if role == 'planner':
    data = {'note': 'Planned', 'new_title': 'Make greeting',
            'new_description': 'Write greeting.txt containing hello. Test exact contents.'}
elif role == 'doer':
    Path('greeting.txt').write_text('hello')
    data = {'success': True, 'note': 'Implemented', 'report': 'Created greeting.txt; contents verified.'}
else:
    ok = Path('greeting.txt').exists() and Path('greeting.txt').read_text() == 'hello'
    data = {'success': ok, 'note': 'Tested', 'report': 'exact contents verified' if ok else 'missing greeting'}
print(json.dumps(data))
