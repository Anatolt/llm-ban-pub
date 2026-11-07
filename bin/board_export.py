"""Read-only, deterministic Markdown snapshots of board cards."""
import html
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile

from storage import db_path


def inline(value):
    """Keep card text from introducing Markdown structure or raw HTML."""
    text = html.escape(' '.join(value.splitlines()), quote=False)
    return re.sub(r'([\\`*_{}\[\]()#+.!|~\-])', r'\\\1', text)


def render(cards, columns, project=None):
    ranks = {column: index for index, column in enumerate(columns)}
    cards = sorted(cards, key=lambda c: (
        ranks.get(c['column_name'], len(ranks)), c['column_name'], c['position'], c['id']))
    lines = ['# LLM BAN board', '']
    if project is not None:
        lines.extend(['Project: ' + (inline(project) or '(unassigned)'), ''])
    if not cards:
        lines.extend(['No cards.', ''])
    for card in cards:
        description = card['description']
        fence = '`' * max(3, 1 + max((len(m) for m in re.findall(r'`+', description)), default=0))
        lines.extend([
            f"## Card #{card['id']}: {inline(card['title'])}", '',
            '- Project: ' + (inline(card['project']) or '(unassigned)'),
            '- Column: ' + inline(card['column_name']),
            '- Created: ' + inline(card['created_at']),
            '- Updated: ' + inline(card['updated_at']), '',
            '### Description', '', fence + 'text', description, fence, '',
        ])
    return '\n'.join(lines)


def export_board(columns, project=None, output='-'):
    source = db_path().resolve()
    target = Path(output).resolve() if output != '-' else None
    if target in {source, Path(str(source) + '-wal'), Path(str(source) + '-shm')}:
        raise ValueError('Export destination must not overwrite the board database or its journal')
    # Do not bootstrap a missing database or run migrations during inspection.
    connection = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=30)
    try:
        connection.row_factory = sqlite3.Row
        query = 'SELECT id,title,description,project,column_name,position,created_at,updated_at FROM cards'
        cards = connection.execute(query + (' WHERE project=?' if project is not None else ''),
                                   (project,) if project is not None else ()).fetchall()
        text = render(cards, columns, project)
    finally:
        connection.close()
    if target is None:
        # Use UTF-8 even on Windows consoles/pipes with a legacy code page.
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8', newline='\n')
        sys.stdout.write(text)
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                                         dir=target.parent, prefix='.llm-ban-export-',
                                         delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
