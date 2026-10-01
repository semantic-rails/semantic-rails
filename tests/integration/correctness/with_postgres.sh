#!/usr/bin/env bash
# Run a command with a throwaway Postgres 16 (SR_POSTGRES_* exported, strict mode on).
# Uses SR_POSTGRES_HOST as is when already set. Without Docker it runs the command
# unchanged, so the Postgres checks skip. Removes only the container it started.
set -euo pipefail
if [ -n "${SR_POSTGRES_HOST:-}" ]; then
  SR_INTEGRATION_STRICT=1 exec "$@"
fi
if ! docker info >/dev/null 2>&1; then
  echo "with_postgres: Docker unavailable; Postgres checks skip" >&2
  exec "$@"
fi
name="sr-correctness-pg-$$"
docker run -d --rm --name "$name" -p 127.0.0.1::5432 -e POSTGRES_USER=sr_test \
  -e POSTGRES_PASSWORD=sr_test_pw -e POSTGRES_DB=sr_correctness \
  "${SR_POSTGRES_IMAGE:-postgres:16@sha256:1a6ab3f5345eb6dbe04a1349529caabdb0ab09293a09590fad07b2246bfa4b54}" >/dev/null
trap 'docker rm -f "$name" >/dev/null 2>&1 || true' EXIT
# Ready over TCP: the image's first-start server listens on a socket only.
for _ in $(seq 60); do
  docker exec "$name" pg_isready -q -h 127.0.0.1 -U sr_test -d sr_correctness && break
  sleep 1
done
docker exec "$name" pg_isready -q -h 127.0.0.1 -U sr_test -d sr_correctness ||
  { echo "with_postgres: Postgres never became ready" >&2; exit 1; }
port="$(docker port "$name" 5432/tcp | head -n1 | sed 's/.*://')"
SR_POSTGRES_HOST=127.0.0.1 SR_POSTGRES_PORT="$port" SR_POSTGRES_USER=sr_test \
  SR_POSTGRES_PASSWORD=sr_test_pw SR_POSTGRES_DATABASE=sr_correctness SR_INTEGRATION_STRICT=1 \
  "$@"
