# Cross-warehouse conformance suite

Runs the same semantic queries (worked examples + the jaffle_shop
package's own test queries) against every registered warehouse and
asserts the results match the DuckDB reference row-for-row, over an
identical JaffleShop fixture.

```bash
make warehouses-up        # local Postgres + ClickHouse
cp .env.example .env      # fill in cloud creds (optional)
source .env
make test-integration     # = uv run pytest -q tests/integration
```

- Not part of the default pytest run (`testpaths` excludes it).
- A warehouse with missing env vars **skips**; unreachable local infra
  also skips (set `SR_INTEGRATION_STRICT=1` to fail instead).
- The fixture is built once per seed-content hash under
  `data/.fixture_cache/` and loaded into each warehouse idempotently
  (fingerprint marker table), so repeat runs don't re-load.
- Adding a warehouse: see `docs/ADDING_A_DIALECT.md`.

## Differential correctness suite (`correctness/`)

`make test-postgres` asks the same questions of DuckDB and a throwaway Postgres 16 container
(removed afterwards), checks each answer against reference SQL, and requires Postgres to
answer as DuckDB does. Without Docker the Postgres checks skip. With `SR_POSTGRES_*` already
set it uses that server, in a schema of its own that it drops afterwards; `make
test-integration` also collects this suite. Known wrong answers are strict xfails that link
their issue.
