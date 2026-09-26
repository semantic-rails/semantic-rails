.PHONY: install lint format typecheck complexity contracts-check changelog-check clean-transient \
	release-check packages test-backend test warehouses-up warehouses-down test-integration \
	test-postgres

# The locked dev environment CI installs; --locked fails on a stale uv.lock.
install:
	uv sync --group dev --locked

# The same checks as CI's lint job, except mypy (see typecheck).
lint:
	uv run ruff check .
	uv run ruff format --check .

# Applies every available fix, then formats; `make lint` reports what is left.
format:
	uv run ruff check --fix --exit-zero .
	uv run ruff format .

typecheck:
	uv run mypy semantic_rails

# Report-only; never fails. Ruff's McCabe complexity, branch and statement
# counts at their default thresholds, then every function over 150 lines.
complexity:
	uv run ruff check semantic_rails mf2sr --select C901,PLR0912,PLR0915 --statistics --exit-zero
	uv run python scripts/dev/function_lengths.py semantic_rails mf2sr

contracts-check:
	uv run python scripts/generate_contract_artifacts.py --check

changelog-check:
	uv run python scripts/changelog_fragments.py check

clean-transient:
	rm -rf build dist .pytest_cache .mypy_cache .ruff_cache
	find . -type d -name "__pycache__" -prune -exec rm -rf {} +
	find . -type d -name "*.egg-info" -prune -exec rm -rf {} +

# Route through uv so the script runs inside the project venv (bare python3
# cannot import semantic_rails).
release-check:
	uv run python scripts/verify_release_readiness.py

packages:
	uv run semantic-rails packages

# The same suites and parallelism as CI's backend job.
test-backend:
	uv run pytest -q tests/semantic_rails tests/mf2sr -n auto

test: test-backend release-check

# Local warehouse infra for the cross-dialect conformance suite
# (tests/integration). Cloud warehouses need creds — see .env.example.
warehouses-up:
	docker compose -f docker-compose.warehouses.yml up -d --wait

warehouses-down:
	docker compose -f docker-compose.warehouses.yml down -v

# Warehouses without env vars (or unreachable infra) skip; the suite
# stays green. `source .env` first to enable the cloud targets.
# Every test here carries the `integration` marker (tests/integration/conftest.py).
test-integration:
	uv run pytest -q tests/integration

# Differential correctness suite (tests/integration/correctness): DuckDB always, plus a
# throwaway Postgres 16 in Docker (removed afterwards; the Postgres checks skip without Docker).
test-postgres:
	tests/integration/correctness/with_postgres.sh uv run --locked --extra postgres pytest -q tests/integration/correctness
