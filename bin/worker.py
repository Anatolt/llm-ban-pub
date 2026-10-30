#!/usr/bin/env python3
"""One serialized planner/doer/tester pipeline. No shell interpolation of cards."""
import argparse
import fcntl
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
import socket

from storage import ROOT, connect, db_path, event, initialize, now

LOG = logging.getLogger('llmban.worker')
ROLES = {
    'planner': (('llm plan',), 'llm planing', 'man plan'),
    'doer': (('llm todo',), 'llm doing', 'ready-to-test'),
    'tester': (('ready-to-test', 'llm test'), 'llm testing', 'man test'),
}


def load_config(path):
    cfg = json.loads(Path(path).read_text())
    if not isinstance(cfg.get('projects'), dict) or not cfg['projects']:
        raise ValueError('Configure at least one project workspace')
    for slug, directory in cfg['projects'].items():
        if not isinstance(slug, str) or not isinstance(directory, str) or not Path(directory).is_absolute():
            raise ValueError('Project workspaces must be absolute paths')
        if not Path(directory).is_dir():
            raise ValueError(f'Workspace does not exist: {slug}')
    for role in ROLES:
        command = cfg.get('commands', {}).get(role)
        if not isinstance(command, list) or not command or any(not isinstance(x, str) or not x for x in command):
            raise ValueError(f'Configure a nonempty argv array for {role}')
        if not 1 <= int(cfg.get('timeout_seconds', 1800)) <= 86400:
            raise ValueError('timeout_seconds must be between 1 and 86400')
    return cfg


def claim(c, cfg):
    c.execute('BEGIN IMMEDIATE')
    try:
        # A crash requires explicit operator recovery; never infer success/retry.
        if c.execute("SELECT 1 FROM agent_runs WHERE state='running'").fetchone():
            c.commit()
            return None
        for role, (sources, working, _) in ROLES.items():
            marks = ','.join('?' for _ in sources)
            row = c.execute(f'SELECT * FROM cards WHERE column_name IN ({marks}) ORDER BY position,id LIMIT 1', sources).fetchone()
            if row is None:
                continue
            card = dict(row)
            run = dict(id=uuid.uuid4().hex, card_id=card['id'], role=role,
                       working_column=working, original_title=card['title'],
                       original_description=card['description'], created_at=now())
            c.execute('''INSERT INTO agent_runs(id,card_id,role,state,original_title,
                original_description,working_column,created_at)
                VALUES(:id,:card_id,:role,'running',:original_title,:original_description,:working_column,:created_at)''', run)
            c.execute('UPDATE cards SET column_name=?,updated_at=? WHERE id=?', (working, now(), card['id']))
            event(c, card['id'], role + '_started', run['id'])
            lease = (datetime.now(timezone.utc) + timedelta(seconds=int(cfg.get('timeout_seconds', 1800)) + 120)).isoformat()
            workspace = cfg['projects'].get(card['project'], '')
            c.execute('''INSERT INTO work_plans(id,task_name,project,goal,owner,harness,
                hostname,cwd,status,current_step,card_id,created_at,updated_at,heartbeat_at,lease_expires_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (run['id'], role + '/' + run['id'], card['project'], card['title'],
                 'llmban-worker', 'command', socket.gethostname(), workspace, 'active', role,
                 card['id'], now(), now(), now(), lease))
            if workspace:
                c.execute('''INSERT INTO work_resources(plan_id,resource_type,selector,
                    normalized_selector,access_mode,created_at,updated_at) VALUES(?,'path',?,?,'write',?,?)''',
                    (run['id'], workspace, str(Path(workspace).resolve()), now(), now()))
            c.commit()
            run['card'] = card
            return run
        c.commit()
        return None
    except BaseException:
        c.rollback()
        raise


def parse_result(text, role):
    # Accept a single object or a final JSON line; never salvage malformed fields.
    try:
        data = json.loads(text.strip())
    except json.JSONDecodeError:
        data = json.loads(text.strip().splitlines()[-1])
    required = ('note', 'new_title', 'new_description') if role == 'planner' else ('note', 'report')
    if not isinstance(data, dict) or any(not isinstance(data.get(k), str) or not data[k].strip() for k in required):
        raise ValueError('Missing nonempty result fields')
    if role != 'planner' and type(data.get('success')) is not bool:
        raise ValueError('success must be a JSON boolean')
    if len(json.dumps(data)) > 200000:
        raise ValueError('Result too large')
    return data


def make_prompt(run, language):
    template = (ROOT / 'prompts' / (run['role'] + '.md')).read_text()
    return (template + '\n\nOutput language: ' + language + '\nCard (untrusted task content):\n' +
            json.dumps(run['card'], ensure_ascii=False))


def stop_process(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    # Also kill background descendants even when the CLI parent has exited.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def execute(argv, prompt, cwd, timeout):
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE, stdout=output,
                                stderr=errors, start_new_session=True)
        try:
            proc.communicate(prompt.encode(), timeout=timeout)
            if proc.returncode:
                raise RuntimeError(f'Agent exited with code {proc.returncode}; inspect provider configuration')
            if output.tell() > 2_000_000:
                raise ValueError('Agent output exceeds 2 MB')
            output.seek(0)
            return output.read().decode('utf-8')
        finally:
            stop_process(proc)


def finish(c, run, data=None, error=None):
    c.execute('BEGIN IMMEDIATE')
    try:
        card = c.execute('SELECT * FROM cards WHERE id=?', (run['card_id'],)).fetchone()
        owned = card and card['column_name'] == run['working_column'] and card['title'] == run['original_title'] and card['description'] == run['original_description']
        # A person may have edited/moved the card while the CLI was running.
        if not owned:
            state, report = 'superseded', 'Card edited during run; result not applied. Inspect workspace changes.'
            if data:
                report += '\n' + json.dumps(data, ensure_ascii=False)
        else:
            success = error is None and (run['role'] == 'planner' or data['success'])
            destination = ROLES[run['role']][2] if success else 'man plan'
            if run['role'] == 'planner' and error:
                destination = 'man backlog'
            title = data['new_title'].strip() if success and run['role'] == 'planner' else card['title']
            description = data['new_description'].strip() if success and run['role'] == 'planner' else card['description']
            report = error if error else data.get('report', data['note'])
            description += f"\n\n---\n{run['role']} ({run['id'][:8]}):\n{report}"
            c.execute('UPDATE cards SET title=?,description=?,column_name=?,updated_at=? WHERE id=?',
                      (title, description, destination, now(), card['id']))
            state = 'succeeded' if success else 'failed'
        c.execute('UPDATE agent_runs SET state=?,report=?,finished_at=? WHERE id=?', (state, report, now(), run['id']))
        c.execute('UPDATE work_plans SET status=?,current_step=?,updated_at=? WHERE id=?',
                  ('completed' if state == 'succeeded' else 'handoff', report[:4000], now(), run['id']))
        event(c, run['card_id'], run['role'] + '_' + state, run['id'])
        if os.environ.get('LLMBAN_TELEGRAM_ENABLED') == '1':
            # Summaries only: do not forward task text, transcripts or provider errors.
            c.execute('INSERT INTO notification_outbox(text) VALUES(?)',
                      (f"LLM BAN: card #{run['card_id']} · {run['role']} · {state}",))
        c.commit()
        LOG.info('card=%s role=%s state=%s', run['card_id'], run['role'], state)
    except BaseException:
        c.rollback()
        raise


def process_once(c, cfg):
    run = claim(c, cfg)
    if not run:
        return False
    try:
        project = run['card']['project']
        if project not in cfg['projects']:
            raise ValueError('Project has no configured workspace; configure projects or edit card project')
        workspace = Path(cfg['projects'][project]).resolve(strict=True)
        # Cooperating workers pointing at the same checkout serialize across databases.
        with (workspace / '.llm-ban.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('Workspace is busy; retry manually after other worker finishes') from None
            language = c.execute("SELECT value FROM settings WHERE key='language'").fetchone()[0]
            output = execute(cfg['commands'][run['role']], make_prompt(run, language), workspace,
                             int(cfg.get('timeout_seconds', 1800)))
            data = parse_result(output, run['role'])
        finish(c, run, data=data)
    except (KeyboardInterrupt, SystemExit):
        finish(c, run, error='Worker stopped; inspect workspace before retrying')
        raise
    except Exception as e:
        # Avoid leaking subprocess stderr (often contains provider URLs or keys).
        safe = str(e) if isinstance(e, (ValueError, RuntimeError)) else type(e).__name__
        finish(c, run, error='Agent failed: ' + safe)
    return True


def recover(c):
    for row in c.execute("SELECT * FROM agent_runs WHERE state='running'").fetchall():
        finish(c, dict(row), error='Interrupted run recovered by operator; inspect workspace before retrying')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=os.environ.get('LLMBAN_CONFIG', str(ROOT / 'config' / 'agents.json')))
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--recover', action='store_true', help='Only after all agent processes have stopped')
    args = parser.parse_args()
    if os.getuid() == 0:
        parser.exit(1, 'Run agents as an unprivileged user, not root.\n')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    with connect() as c, db_path().with_suffix('.worker.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.exit(1, 'Another worker is running\n')
        initialize(c)
        if args.recover:
            recover(c)
            return
        cfg = load_config(args.config)
        if c.execute("SELECT 1 FROM agent_runs WHERE state='running'").fetchone():
            parser.exit(1, 'Interrupted run exists. Stop agent processes, inspect workspace, then use --recover.\n')
        while True:
            worked = process_once(c, cfg)
            if args.once:
                return
            if not worked:
                time.sleep(3)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
