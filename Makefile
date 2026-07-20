.PHONY: clean-transient release-check packages test-backend test \
	warehouses-up warehouses-down test-integration

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

test-backend:
	uv run pytest -q tests/semantic_rails

test: test-backend release-check

# Local warehouse infra for the cross-dialect conformance suite
# (tests/integration). Cloud warehouses need creds — see .env.example.
warehouses-up:
	docker compose -f docker-compose.warehouses.yml up -d --wait

warehouses-down:
	docker compose -f docker-compose.warehouses.yml down -v

# Warehouses without env vars (or unreachable infra) skip; the suite
# stays green. `source .env` first to enable the cloud targets.
test-integration:
	uv run pytest -q tests/integration
