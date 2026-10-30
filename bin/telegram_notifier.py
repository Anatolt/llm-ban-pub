#!/usr/bin/env python3
"""Optional outbound Telegram summaries. No incoming commands or bot polling."""
import json
import logging
import os
import time
import urllib.request
from urllib.parse import urlparse

from storage import connect, initialize, now

LOG = logging.getLogger('llmban.telegram')


def deliver_once(c, send, board_url=''):
    row = c.execute('''SELECT * FROM notification_outbox
        WHERE sent_at IS NULL AND next_attempt<=? ORDER BY id LIMIT 1''', (time.time(),)).fetchone()
    if row is None:
        return False
    try:
        send(row['text'] + ('\n' + board_url if board_url else ''))
    except Exception:
        attempts = row['attempts'] + 1
        with c:
            c.execute('UPDATE notification_outbox SET attempts=?,next_attempt=? WHERE id=?',
                      (attempts, time.time() + min(3600, 2 ** min(attempts, 11)), row['id']))
        # urllib errors can contain the token in the URL; never log exception text.
        LOG.warning('Telegram delivery failed; retained for retry')
    else:
        with c:
            c.execute('UPDATE notification_outbox SET sent_at=? WHERE id=?', (now(), row['id']))
    return True


def main():
    logging.basicConfig(level=logging.INFO)
    if os.environ.get('LLMBAN_TELEGRAM_ENABLED') != '1':
        raise SystemExit('Telegram disabled; set LLMBAN_TELEGRAM_ENABLED=1 to enable')
    token = os.environ.get('LLMBAN_TELEGRAM_TOKEN', '')
    chat = os.environ.get('LLMBAN_TELEGRAM_CHAT_ID', '')
    if not token or not chat:
        raise SystemExit('Set LLMBAN_TELEGRAM_TOKEN and LLMBAN_TELEGRAM_CHAT_ID')
    board = os.environ.get('LLMBAN_PUBLIC_URL', '').rstrip('/')
    parsed = urlparse(board)
    if board and (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password):
        raise SystemExit('LLMBAN_PUBLIC_URL must be HTTPS without credentials')

    def send(text):
        request = urllib.request.Request(
            f'https://api.telegram.org/bot{token}/sendMessage',
            data=json.dumps({'chat_id': chat, 'text': text, 'disable_web_page_preview': True}).encode(),
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=15) as response:
            if not json.load(response).get('ok'):
                raise RuntimeError('Telegram rejected delivery')

    c = connect()
    try:
        initialize(c)
        while True:
            if not deliver_once(c, send, board):
                time.sleep(3)
    finally:
        c.close()


if __name__ == '__main__':
    main()
