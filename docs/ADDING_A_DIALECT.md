# Adding a warehouse dialect

Adding a warehouse to semantic-rails is exactly three production
pieces — **one dialect class + one adapter + one registry entry** —
plus a small integration target + fixture loader pair so the
conformance suite covers it automatically. Everything else (option
validation, secret resolution, factory dispatch, config validation,
fixture loading, parity testing) is shared machinery that picks the
new warehouse up from the registry.

This guide uses **Redshift** as the worked example because it is the
next connector scheduled to land (the env-var names are already
reserved in `.env.example`).

## 1. Dialect class — `semantic_rails/dialects.py`

Subclass `SqlDialect` and override only what differs from the portable
defaults (the base emits `DATE_TRUNC('grain', ts)`,
`DATE_DIFF('unit', a, b)`, `IS NOT DISTINCT FROM`, `QUANTILE_CONT`,
`arg_min`/`arg_max`, `<AGG>(CASE WHEN … END)`):

```python
@dataclass(frozen=True)
class RedshiftDialect(SqlDialect):
    name: str = "redshift"

    def percentile_cont(self, expr, percentile):
        # Redshift: PERCENTILE_CONT(p) WITHIN GROUP (ORDER BY expr)
        return SqlWithinGroup(
            SqlCall("PERCENTILE_CONT", [SqlLiteral(percentile)]),
            order_by=[SqlOrderTerm(expr=expr, direction="ASC")],
        )

    def median(self, expr):
        return SqlCall("MEDIAN", [expr])

    def date_diff(self, unit, start_expr, end_expr):
        # Redshift: DATEDIFF(unit, start, end)
        return SqlCall(
            "DATEDIFF",
            [SqlDatePart(unit), self.timestamp_cast(start_expr), self.timestamp_cast(end_expr)],
        )

    def date_add(self, unit, value_expr, date_expr):
        return SqlCall("DATEADD", [SqlDatePart(unit), value_expr, date_expr])

    def first_value(self, expr, order_expr):
        # No MIN_BY/ARG_MIN — follow the shipped sorted-ARRAY_AGG
        # precedent (PostgresDialect._value_by indexes
        # ARRAY_AGG(expr ORDER BY order); BigQueryDialect._array_agg_edge
        # is the OFFSET variant), use a windowed form, or document the
        # capability gap in capabilities().
        ...
```

Method-by-method checklist (compare against the warehouse docs):

- [ ] `date_trunc` — arg order and unit spelling (BigQuery reverses it)
- [ ] `date_diff` / `date_add` — function name, arg order, unit-as-keyword vs string
- [ ] `percentile_cont` / `median` — native, `WITHIN GROUP`, or rebuilt
      exactly from sorted `ARRAY_AGG` (the BigQuery/Athena precedent —
      approximate sketches like `APPROX_QUANTILES`/`APPROX_PERCENTILE`
      break parity with the DuckDB reference)
- [ ] `first_value` / `last_value` — `MIN_BY`/`MAX_BY`, `arg_min`, or
      the sorted-`ARRAY_AGG` indexing fallback (postgres/bigquery
      precedent)
- [ ] `null_safe_eq` — `IS NOT DISTINCT FROM`, `EQUAL_NULL`, `<=>`, or CASE fallback
- [ ] `conditional_aggregate` — native `COUNT_IF`-style forms (optional; the
      portable CASE default always works)
- [ ] `timestamp_type_name` / `convert_timezone`
- [ ] `capabilities()` — advertise what the warehouse can and can't do

## 2. Adapter — `semantic_rails/db_parts/redshift.py`

Implement the `WarehouseAdapter` contract. If the driver is DB-API
(PEP 249) — psycopg, redshift_connector, PyAthena, databricks-sql —
subclass `DbApiAdapter` from `semantic_rails.db_parts.common` and you
only write `_create_connection()` plus the timeout hooks:

```python
from .common import (
    DbApiAdapter,
    float_nullif_divisions,
    import_driver,
    normalize_connection_options,
    option_or_env,
    require_missing_env,
    secret_value,
)
from ..dialects import REDSHIFT_CONNECTION_OPTIONS
from ..errors import SemanticLayerError


class RedshiftAdapter(DbApiAdapter):
    engine = "redshift"
    connection_kind = "redshift_native"
    supports_statement_timeout = True  # SET statement_timeout

    def __init__(self, options):
        super().__init__()
        self.options = normalize_connection_options(
            "redshift", self.connection_kind, options or {},
            REDSHIFT_CONNECTION_OPTIONS, label="Redshift",
        )

    def _create_connection(self):
        driver = import_driver(
            "redshift_connector", extra="redshift",
            engine=self.engine, connection_kind=self.connection_kind,
        )
        missing: list[str] = []
        host = option_or_env(self.options, "host", missing)
        user = option_or_env(self.options, "user", missing)
        password = secret_value(
            "password", self.options.get("password_env", ""),
            self.options.get("password_file", ""), missing,
            engine=self.engine, connection_kind=self.connection_kind, label="Redshift",
        )
        require_missing_env(missing, engine=self.engine,
                            connection_kind=self.connection_kind, label="Redshift")
        return driver.connect(host=host, user=user, password=password, ...)

    def _apply_statement_timeout(self, cursor, timeout_seconds):
        cursor.execute(f"SET statement_timeout = {timeout_seconds * 1000}")

    def _reset_statement_timeout(self, cursor):
        cursor.execute("RESET statement_timeout")

    def query(self, sql, *, limits=None):
        # Redshift truncates integer division like Postgres/Trino —
        # reuse the shared ratio-guard compat pass (see "Compat passes").
        return super().query(
            float_nullif_divisions(sql, cast_type="DOUBLE PRECISION"), limits=limits
        )


def create_adapter(package, *, db_path=""):
    """Registry entry point (see dialects.py)."""
    kind = str(package.connection.kind or "").strip()
    if kind != "redshift_native":
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"Unsupported Redshift connection kind '{kind}'",
            details={"engine": "redshift", "connection_kind": kind},
        )
    return RedshiftAdapter(package.connection.options)
```

Non-DB-API drivers (BigQuery client, clickhouse-connect) subclass
`WarehouseAdapter` directly but still reuse the shared machinery —
`normalize_connection_options`, `secret_value`, `env_value`,
`option_or_env` (literal-or-`*_env` locator resolution), `int_option`,
`import_driver`, `require_missing_env`, `bounded_error_text`,
`redacted_error_details`, `rows_from_cursor`, and the compat-pass
helpers `float_nullif_divisions` / `rewrite_double_quoted_identifiers` /
`map_double_quoted_identifiers` from `db_parts.common`, plus
`_clip_rows` / `_limit_timeout_seconds` / `_limit_timeout_milliseconds` from
`db_parts.base` — never
copy-paste them.

**Compat passes.** When the warehouse has a hard limit the portable
SQL AST cannot express, the adapter may rewrite the compiler's
rendered SQL just before execution — the postgres adapter is the
precedent (`_postgres_compat_sql`: 63-byte identifier shortening for
NAMEDATALEN truncation + float-casting `x / NULLIF(y, 0)` so integer
ratios don't truncate). Four shipped passes to compare:
`_postgres_compat_sql`, `_bigquery_compat_sql` (backtick re-quoting +
deterministic legalization of illegal field names, with result-row
keys mapped back to the original aliases), `_databricks_compat_sql`
(shared backtick rewrite + float NULLIF division), and
`_athena_compat_sql` (`TIMESTAMP`-typing bare ISO literals in
comparisons + float NULLIF division). Three rules: every rewrite is
**semantics-preserving**, **literal-aware** (a quote-aware scan copies
single-quoted string literals verbatim — the compiler renders string
literals with single quotes only), and **documented** with the exact
warehouse limit it compensates for. Backtick dialects reuse the shared
`rewrite_double_quoted_identifiers` helper from `db_parts.common`
instead of writing their own scan (BigQuery is the one exception: its
scan also has to legalize field names, so it carries its own).

Rules every adapter follows:

- **Secrets** come from env-var indirection (`*_env`) or files
  (`*_file`); `normalize_connection_options` rejects literal
  `password:`/`token:`/… keys.
- **Errors** are redacted: engine, connection kind, option KEYS, and
  bounded driver text only — never option values, raw SQL, or rows.
- **Drivers are optional extras** (`pyproject.toml`); import via
  `import_driver` so a missing driver maps to `MISSING_DEPENDENCY`
  naming the extra.

## 3. Registry entry — `semantic_rails/dialects.py`

Define the connection-option tuple and register the connector. The
commented Redshift block next to `_WAREHOUSE_CONNECTORS` is this step,
ready to uncomment:

```python
REDSHIFT_CONNECTION_OPTIONS: tuple[str, ...] = (
    "host", "host_env", "port", "database", "schema",
    "user", "user_env", "password_env", "password_file",
    "statement_timeout_seconds",
)

"redshift": WarehouseConnectorSpec(
    name="redshift",
    dialect=RedshiftDialect(),
    connection_kinds=("redshift_native",),
    connection_options=REDSHIFT_CONNECTION_OPTIONS,
    adapter="semantic_rails.db_parts.redshift:create_adapter",
),
```

That single entry wires the warehouse into `supported_warehouses()`,
`dialect_for_warehouse()`, package-config validation
(`connection_option_errors`), and `create_warehouse_adapter()` — no
other production file changes. Add the driver extra to
`pyproject.toml` (`redshift = ["redshift-connector>=2.1"]`, plus the
`all` list).

## 4. Integration target — `tests/integration/targets/redshift.py`

Secrets and required locators use `*_env` indirection; optional
locators read their defaults from the environment at import time
(mirroring `.env.example`), matching the shipped targets:

```python
import os

from ..harness import IntegrationTarget
from ..loaders.redshift import RedshiftFixtureLoader

TARGET = IntegrationTarget(
    warehouse="redshift",
    connection_kind="redshift_native",
    connection_options={
        "host_env": "SR_REDSHIFT_HOST",
        "port": os.environ.get("SR_REDSHIFT_PORT", "5439"),
        "database": os.environ.get("SR_REDSHIFT_DATABASE", "sr_jaffle"),
        "user_env": "SR_REDSHIFT_USER",
        "password_env": "SR_REDSHIFT_PASSWORD",
    },
    required_env=("SR_REDSHIFT_HOST", "SR_REDSHIFT_USER", "SR_REDSHIFT_PASSWORD"),
    make_loader=RedshiftFixtureLoader,
    notes="Redshift Serverless or provisioned; standard INSERT DML.",
)
```

The loader (`tests/integration/loaders/redshift.py`) is usually a
`LiteralInsertLoader` subclass with a per-warehouse DDL type map —
compare `loaders/postgres.py`; warehouses with a bulk path (load jobs,
COPY, external tables) override `FixtureLoader.load_table` instead:

```python
from . import LiteralInsertLoader


class RedshiftFixtureLoader(LiteralInsertLoader):
    type_map = {
        "integer": "BIGINT",
        "float": "DOUBLE PRECISION",
        "decimal": "DECIMAL(18,3)",
        "string": "VARCHAR(MAX)",
        "date": "DATE",
        "timestamp": "TIMESTAMP",
        "boolean": "BOOLEAN",
    }
```

`test_registry_targets_complete` fails the moment a warehouse is
registered without a target module, and the parametrized conformance
suite (`test_semantic_surface_path`, `test_battery_parity`) picks the
new target up automatically. Targets whose `required_env` vars are
unset skip; unreachable/unloadable infra also skips with a loud
message unless `SR_INTEGRATION_STRICT=1` (CI posture) — which is why
Redshift can ship as *registry-ready stub docs* today and go live by
uncommenting one block, adding two files, and filling in
`SR_REDSHIFT_*` in `.env`.

## Checklist

- [ ] Dialect class with quirk overrides (`semantic_rails/dialects.py`)
- [ ] Adapter module exposing `create_adapter` (`semantic_rails/db_parts/<wh>.py`)
- [ ] Registry entry + option tuple (`semantic_rails/dialects.py`)
- [ ] Driver extra in `pyproject.toml` (+ `all`)
- [ ] Env var names documented in `.env.example`
- [ ] Integration target (`tests/integration/targets/<wh>.py`)
- [ ] Fixture loader (`tests/integration/loaders/<wh>.py`)
- [ ] `make test-integration` (= `uv run pytest -q tests/integration`) —
      conformance parity green (or env-gated skip)
- [ ] Unit rendering tests for the dialect quirks
      (`tests/semantic_rails/test_dialect_<wh>.py`)
- [ ] Compat-pass rewrites (if any) documented and literal-aware
