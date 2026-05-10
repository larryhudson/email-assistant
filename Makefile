.PHONY: dev db-up db-down migrate test

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
