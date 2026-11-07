#!/usr/bin/env python3
"""Initialize storage, add cards, export Markdown and inspect runs without HTTP credentials."""
import argparse
import json
import sqlite3
from storage import connect, initialize, event, next_position, now

COLUMNS = ['man backlog', 'llm plan', 'llm planing', 'preflight', 'man plan',
           'llm todo', 'llm doing', 'ready-to-test', 'llm test', 'llm testing',
           'man test', 'man done', 'man canceled']


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='action', required=True)
    sub.add_parser('init')
    create = sub.add_parser('create')
    create.add_argument('--title', required=True)
    create.add_argument('--description', default='')
    create.add_argument('--project', default='')
    create.add_argument('--column', default='man backlog', choices=COLUMNS)
    sub.add_parser('runs')
    export = sub.add_parser('export', help='Export a read-only Markdown snapshot')
    export.add_argument('--project', help='Filter by exact project slug; empty string selects unassigned cards')
    export.add_argument('--output', default='-', help='Destination file (parent must exist), or - for UTF-8 stdout')
    args = p.parse_args()
    if args.action == 'export':
        import re
        from board_export import export_board
        if args.project and not re.fullmatch('[a-z0-9][a-z0-9-]{0,63}', args.project):
            p.error('Invalid project slug')
        try:
            export_board(COLUMNS, args.project, args.output)
        except (OSError, sqlite3.Error, ValueError) as exc:
            p.exit(1, f'Export failed: {exc}\n')
        return
    c = connect()
    try:
        initialize(c)
        if args.action == 'create':
            import re
            if not args.title.strip() or len(args.title) > 1000:
                p.error('Title must contain 1–1000 characters')
            if args.project and not re.fullmatch('[a-z0-9][a-z0-9-]{0,63}', args.project):
                p.error('Invalid project slug')
            with c:
                cursor = c.execute('''INSERT INTO cards(title,description,project,column_name,position,
                    created_at,updated_at) VALUES(?,?,?,?,?,?,?)''',
                    (args.title, args.description, args.project, args.column,
                     next_position(c, args.column), now(), now()))
                event(c, cursor.lastrowid, 'create', 'cli')
            print(json.dumps({'id': cursor.lastrowid}))
        elif args.action == 'runs':
            print(json.dumps([dict(r) for r in c.execute('SELECT * FROM agent_runs ORDER BY created_at DESC LIMIT 100')], indent=2))
        else:
            print('Storage initialized')
    finally:
        c.close()


if __name__ == '__main__':
    main()
