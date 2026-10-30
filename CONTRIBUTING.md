# Contributing

Use issues for bug reports and proposals, and pull requests for changes. Fork
the repository, branch from `main`, and keep each PR focused on one behavior.
Include the problem, resulting behavior and validation evidence in the PR.
Please discuss changes to task execution permissions or schema before coding.

## Local checks

Requires Python 3.11+ and PHP 8.2+ with PDO SQLite. There are no pip dependencies.

```sh
python3 -m unittest discover -s tests -v
php -l public/index.php
php -l public/bootstrap.php
php -l public/router.php
```

The suite starts PHP on a temporary loopback port and uses temporary databases
and an offline fixture agent. It never needs production data or provider keys.
Docker integration: `python3 scripts/smoke_compose.py` builds both images and
checks the actual Apache/SQLite/worker stack with a disposable workspace. It
stops its containers afterwards and preserves its test volume for inspection.

## Review expectations

- Schema/config changes need upgrade notes and a migration path.
- Worker changes need tests for failed runs, interruption and human edits.
- Keep task content out of shell interpolation and provider errors out of logs.
- Do not commit `.env`, databases, backups, real task content or API credentials.
- Use ordinary English for public docs and diagnostics. Preserve the existing
  column names and support Russian/English agent output.

CI must pass and a maintainer must review changes to `main`. Do not publish
private security reports as public issues; follow SECURITY.md. Contributions
are licensed under the project's MIT license. No CLA is required.
