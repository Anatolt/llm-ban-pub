# Working on LLM BAN

This repository is a standalone public project. Do not assume any private SSH
hosts, memory directories, credentials or services exist. Runtime configuration
lives in ignored `.env` and `config/agents.json`; defaults/examples must contain
no credentials. Keep the web document root limited to `public/`.

Run `python3 -m unittest discover -s tests -v` and PHP lint for PHP edits.
Use `python3 scripts/smoke_compose.py` for Docker/permissions/routing changes.
Do not call live LLMs or send Telegram messages in tests. Preserve human approval
gates, explicit interruption recovery, strict result validation and safe process
termination. Never auto-push, deploy, or mark partially verified work successful.

Do not copy private runtime data into this repository. Do not delete user
workspaces, backups or volumes. Add schema migration/operations notes when
changing stored formats. Follow CONTRIBUTING.md and SECURITY.md.
