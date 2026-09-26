#!/usr/bin/env bash
# Run a command with a throwaway Postgres 16 (SR_POSTGRES_* exported, strict mode on).
# Uses SR_POSTGRES_HOST as is when already set. Without Docker it runs the command
# unchanged, so the Postgres checks skip. Removes only the container it started.
set -euo pipefail
if [ -n "${SR_POSTGRES_HOST:-}" ] || ! docker info >/dev/null 2>&1; then
  [ -n "${SR_POSTGRES_HOST:-}" ] || echo "with_postgres: Docker unavailable; Postgres checks skip" >&2
  exec "$@"
fi
name="sr-correctness-pg-$$"
docker run -d --rm --name "$name" -p 127.0.0.1::5432 -e POSTGRES_USER=sr_test \
  -e POSTGRES_PASSWORD=sr_test_pw -e POSTGRES_DB=sr_correctness \
  "${SR_POSTGRES_IMAGE:-postgres:16}" >/dev/null
trap 'docker rm -f "$name" >/dev/null 2>&1 || true' EXIT
# Ready over TCP: the image's first-start server listens on a socket only.
for _ in $(seq 60); do
  docker exec "$name" pg_isready -q -h 127.0.0.1 -U sr_test -d sr_correctness && break
  sleep 1
done
port="$(docker port "$name" 5432/tcp | head -n1 | sed 's/.*://')"
SR_POSTGRES_HOST=127.0.0.1 SR_POSTGRES_PORT="$port" SR_POSTGRES_USER=sr_test \
  SR_POSTGRES_PASSWORD=sr_test_pw SR_POSTGRES_DATABASE=sr_correctness SR_INTEGRATION_STRICT=1 \
  "$@"
