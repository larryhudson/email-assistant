.PHONY: dev db-up db-down migrate test worktree-up worktree-down

# Bring up the dev process stack: Postgres (docker), web + worker (hivemind).
# Web binds to 127.0.0.1:18788; worker sends real Mailgun replies by default.
# Set EMAIL_AGENT_WORKER_DRY_RUN=true on the worker line in Procfile.dev to
# suppress sends during local testing.
dev: db-up migrate
	hivemind Procfile.dev 2>&1 | tee dev.log

# Postgres only — `make dev` already calls this; here for ad-hoc use.
db-up:
	docker compose up -d postgres
	@echo "waiting for postgres to be ready..."
	@until docker compose exec -T postgres pg_isready -U email_agent >/dev/null 2>&1; do \
		sleep 0.5; \
	done

db-down:
	docker compose down

migrate:
	uv run alembic upgrade head

test:
	uv run pytest tests/unit -q

# Spin up an isolated dev worktree on its own branch + DB.
# Usage: make worktree-up name=foo [port=18789]
worktree-up:
	@./scripts/dev-worktree-up "$(name)" "$(port)"

# Tear down a dev worktree. Pass drop_db=1 to also DROP the postgres database.
# Usage: make worktree-down name=foo [drop_db=1]
worktree-down:
	@./scripts/dev-worktree-down "$(name)" $(if $(drop_db),--drop-db,)
