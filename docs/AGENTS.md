# Agent integration contract

`config/agents.json` is trusted administrator configuration. It maps exact card
project slugs to absolute workspace directories and each role to an argv array.
The worker starts that command in the mapped directory with the complete prompt
on **stdin**. Card content is never interpolated into a shell command.

The bundled Pi adapter reads stdin and invokes Pi print mode. It enables only
read/search tools for planning; doer/tester have coding and shell tools. These
tool selections and prompts are not an OS sandbox. The worker is intended for
trusted projects and trusted people who may queue code execution.

A custom adapter may call any coding agent, but must:

- Read the task from stdin and emit UTF-8 text on stdout.
- Exit nonzero on provider/runtime failure.
- End stdout with a single-line JSON object, with no output after it. A lone
  multiline JSON object is also accepted. Markdown fences are rejected.
- Keep output under 2 MB and complete within `timeout_seconds` (1–86400).
- Keep child processes in its process group; no daemonization/detached jobs.
- Avoid printing credentials. Raw stderr is intentionally not copied to cards.

Planner result:

```json
{"note":"Planned","new_title":"Implement feature","new_description":"Scope, acceptance criteria, tests and open questions"}
```

Doer/tester result:

```json
{"success":true,"note":"Verified","report":"What changed; acceptance evidence; commands/results; human verification; risks"}
```

`success` must be a boolean, never a string. Required string fields must be
nonempty. All fields should use the output language selected in board Settings.
The wrapper chooses destinations; an agent cannot choose an arbitrary column.

One worker handles one card at a time across all projects. SQLite prevents
double claims; a database lock prevents two worker daemons and workspace file
locks serialize cooperating LLMBAN workers using the same checkout. These
mechanisms cannot coordinate unrelated editors or arbitrary external agents.

Humans may edit/move a running card. The worker then records a `superseded` run
and leaves the edited card intact. Workspace changes already performed by the
agent remain; moving a card does not cancel a running process. Stop the worker
for cancellation and inspect its diff before retrying.

Interrupted persisted runs require explicit recovery. The wrapper never resumes
or silently reruns an unfinished implementation. See OPERATIONS.md.
