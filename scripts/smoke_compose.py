#!/usr/bin/env python3
"""Build/test an isolated Compose instance with an offline agent, then stop it."""
import base64
import http.cookiejar
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]


def main():
    project = 'llm-ban-test-' + uuid.uuid4().hex[:10]
    # Keep fixture/config outside the repository and never read the user's .env.
    with tempfile.TemporaryDirectory(prefix='llm-ban-smoke-') as temp:
        root = Path(temp)
        root.chmod(0o755)
        workspace = root / 'workspaces' / 'demo'
        workspace.mkdir(parents=True)
        uid = os.getuid() if os.getuid() else 1000
        gid = os.getgid() if os.getuid() else 1000
        if os.getuid() == 0:
            os.chown(workspace, uid, gid)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        envfile = root / 'test.env'
        password = uuid.uuid4().hex
        envfile.write_text(f'LLMBAN_USERNAME=smoke\nLLMBAN_PASSWORD={password}\nLLMBAN_PORT={port}\n'
                           f'LLMBAN_UID={uid}\nLLMBAN_GID={gid}\nLLMBAN_WORKSPACES={workspace.parent}\n')
        cfg = {'projects': {'demo': '/workspaces/demo'}, 'timeout_seconds': 10,
               'commands': {r: ['python3', '/fixtures/fixture_agent.py', r] for r in ['planner', 'doer', 'tester']}}
        config = root / 'agents.json'
        config.write_text(json.dumps(cfg))
        # Override the ignored local config mount by target, and mount test fixture.
        override = root / 'compose.test.json'
        override.write_text(json.dumps({'services': {'worker': {'volumes': [
            f'{config}:/app/config/agents.json:ro', f'{ROOT / "tests"}:/fixtures:ro']}}}))
        compose = ['docker', 'compose', '--env-file', str(envfile), '-p', project,
                   '-f', str(ROOT / 'compose.yaml'), '-f', str(override), '--profile', 'agents']
        # Shell env must not override disposable configuration with real credentials.
        env = {k: v for k, v in os.environ.items() if not k.startswith('LLMBAN_') and k not in
               {'ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'GEMINI_API_KEY', 'OPENROUTER_API_KEY'}}
        def run(*args, capture=False):
            return subprocess.run(compose + list(args), cwd=ROOT, env=env, check=True,
                                  text=True, stdout=subprocess.PIPE if capture else None)
        url = f'http://127.0.0.1:{port}'
        auth = 'Basic ' + base64.b64encode(('smoke:' + password).encode()).decode()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        def request(path='/', data=None):
            req = urllib.request.Request(url + path, data=urllib.parse.urlencode(data).encode() if data else None,
                                         headers={'Authorization': auth})
            with opener.open(req, timeout=5) as response:
                return response.read().decode()
        def wait_column(column):
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                body = request('/api/cards')
                try:
                    cards = json.loads(body)['cards']
                except json.JSONDecodeError:
                    raise AssertionError('API returned non-JSON: ' + body[:500]) from None
                if cards and cards[0]['column_name'] == column:
                    return cards[0]
                time.sleep(.5)
            raise AssertionError('Card did not reach ' + column)
        try:
            run('up', '-d', '--build', 'web', 'worker')
            deadline = time.monotonic() + 45
            while True:
                try:
                    request('/')
                    break
                except (OSError, urllib.error.URLError):
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(.5)
            try:
                urllib.request.urlopen(url + '/api/cards', timeout=5)
                raise AssertionError('Unauthenticated API was accessible')
            except urllib.error.HTTPError as error:
                assert error.code == 401
                error.close()
            token = re.search('name="csrf" value="([a-f0-9]+)"', request('/?action=new'))[1]
            request(data={'csrf': token, 'action': 'create', 'title': 'Greeting', 'description': 'Write greeting',
                          'project': 'demo', 'column_name': 'llm plan'})
            card = wait_column('man plan')
            request(data={'csrf': token, 'action': 'move', 'id': card['id'], 'column_name': 'llm todo'})
            wait_column('man test')
            assert (workspace / 'greeting.txt').read_text() == 'hello'
            run('restart', 'web')
            # Container restart must preserve the named-volume database.
            time.sleep(2)
            wait_column('man test')
            run('exec', '-T', 'worker', 'python3', 'bin/backup.py')
            version = run('exec', '-T', 'worker', 'pi', '--version', capture=True).stdout.strip()
            assert version == '1.4.0', version
            print('PASS: authentication, empty DB, planner/doer/tester, permissions, persistence, backup, Pi ' + version)
        finally:
            run('logs', '--tail=30', 'web', 'worker')
            run('down')  # Keep the named volume: no automatic data deletion.
            print('Stopped test containers; preserved Docker volume ' + project + '_data')


if __name__ == '__main__':
    main()
