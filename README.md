# email-assistant

An AI assistant you talk to by email, with its own persistent workspace for
files, scripts and tools.

Email is a familiar interface, and much of the context an assistant needs
already arrives in your inbox.

Built with an LLM coding agent. This is a personal experiment, not a
production-ready service.

## How it works

Mailgun forwards incoming email to a FastAPI webhook. The API checks the
signature and sender, saves the message in Postgres and queues an agent run
using Procrastinate.

A worker checks the assistant's budget, prepares its Docker workspace and runs
a PydanticAI agent with access to files and shell commands. The final response
is sent back as an email. Runs execute one at a time per assistant; workspace
files persist between runs.

There's also an admin UI for inspecting runs and usage, scheduled tasks,
optional Cognee memory, and integrations including Google Calendar and GitHub.

For the main flow, start with
[`assistant_runtime.py`](src/email_agent/runtime/assistant_runtime.py).
[`composition.py`](src/email_agent/composition.py) wires the service adapters;
[`sandbox/`](src/email_agent/sandbox/) handles workspace execution.

## Run locally

Requires Python 3.13, [uv](https://docs.astral.sh/uv/), Docker and
[Hivemind](https://github.com/DarthSim/hivemind).

```bash
uv sync --locked
cp .env.example .env
# Fill in credentials and change the admin password in .env.
make sandbox-build
make dev
```

This starts Postgres, applies migrations, and runs the API and worker.
The web server defaults to `http://127.0.0.1:18788`.

Live email needs a Mailgun route pointing to a public HTTPS URL for
`/webhooks/mailgun`. Model calls use Fireworks by default; optional memory
needs its own model/embedding credentials. See `.env.example` and
[`config.py`](src/email_agent/config.py).

Create an assistant before sending mail. You can also inject a local `.eml` file:

```bash
uv run email-agent seed-assistant --help
uv run email-agent inject-email --help
```

## Checks

The unit tests use fake services and need no API keys, Postgres or Docker:

```bash
uv run pytest tests/unit -q
uv run ruff check
uv run ruff format --check
uv run ty check
```

Some tests are marked `xfail` for known gaps, including older direct-tool
expectations and an unimplemented admin workspace browser. Integration tests
have separate service requirements.

## Limitations

- Agents can execute arbitrary commands and access the network. There is no
  outbound domain allowlist or general protection against prompt injection.
- Budgets are checked before a run, so a run can exceed the remaining allowance.
- Saving state, queueing jobs and sending replies aren't one atomic operation.
  Failures between these steps need better recovery and duplicate handling.

The workspace makes bespoke capabilities possible, but their correctness still
needs checking. For non-technical users, I'd lean toward a smaller set of tested
capabilities with explicit permissions.
