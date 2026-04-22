.PHONY: help dev up down logs test lint format migrate migration backup clean

help:  ## Show this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

dev:          ## Run app locally (no Docker) — faster feedback loop
	uv run python -m mib.main

up:           ## docker compose up -d + build si falta
	docker compose up -d --build

down:         ## docker compose down
	docker compose down

logs:         ## Follow container logs
	docker compose logs -f mib

test:         ## Run tests with coverage
	uv run pytest --cov=src/mib --cov-report=term-missing

lint:         ## Lint (ruff) + type check (mypy strict en módulos críticos)
	uv run ruff check .
	uv run mypy src/mib/ai src/mib/sources/base.py src/mib/services

format:       ## Format + auto-fix con ruff
	uv run ruff format .
	uv run ruff check --fix .

migrate:      ## Aplica migraciones pendientes
	uv run alembic upgrade head

migration:    ## Crea nueva migración autogenerada. Ejemplo: make migration m="add_users_email"
	uv run alembic revision --autogenerate -m "$(m)"

backup:       ## Backup manual de la DB
	./scripts/backup.sh

clean:        ## Borra caches locales (no toca .venv, data, ni git)
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov
