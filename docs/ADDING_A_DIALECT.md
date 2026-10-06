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

### ADBC profiles

Postgres uses `AdbcAdapter` with `POSTGRES_PROFILE`, selected by the existing
`connection.kind: postgres_native` and its validated option vocabulary. Install
`semantic-rails[postgres]` (or `[all]`) for the pinned ADBC manager and Postgres
driver plus PyArrow. PyArrow supplies DB-API parameter binding, bounded batch
conversion and PostgreSQL extension type metadata; converting through DuckDB
would add another type-conversion boundary.

A profile names the driver, connection kind and allowed options. Postgres is
qualified; Snowflake is an opt-in profile described below. Other profiles are
refused. Postgres credentials stay in the in-memory libpq connection string,
with keyword values escaped; its packages cannot choose a native library path.
The `schema` option selects one exact, case-sensitive schema name.

The compiler finalizes typed row-filter slots as Postgres `$1`, `$2`, … before
execution. The adapter checks slot types, counts and placeholder order before
connecting, binds values separately and sends prepared SQL unchanged. In the
compiler's ANSI SQL, every placeholder corresponds to one authored slot. Direct
parameterized calls use the same strict check and refuse `?` JSON operators;
plain SQL without slots allows those operators and refuses unbound `$n` tokens.
The shared finalization and validation scanner skips complete Postgres identifiers
(including `$` suffixes), E-string backslash escapes, ordinary quoted text,
dollar-quoted literals and nested comments when locating standalone placeholders.
Arrow batches are sliced before Python row conversion to at most
`max_rows + 1`, with `QueryRows.truncated` and semantic aliases preserved. The
64 KiB driver batch hint bounds typical batches, not arbitrarily large cells.

NUMERIC returns `Decimal` with its scale intact, including aggregate results;
numeric-looking TEXT stays text. Positional correctness reads share this
Arrow-type conversion while retaining duplicate column names. Aware timestamps retain
microseconds and use the requested query zone (otherwise the current session
zone, falling back to aware UTC when Python cannot load it). PostgreSQL stores
instants, so the originally authored offset cannot be recovered. Arrow
`MonthDayNano` intervals become Python `timedelta` values, matching DuckDB's
driver convention of 30 days per month and preserving exact microseconds.
Their public JSON values and interval metadata therefore match DuckDB.
Sub-microsecond intervals or durations outside Python's range refuse with
`RESULT_VALUE_UNSUPPORTED`. Each query restores the session's previous zone. The
adapter preserves inherited statement timeouts unless a request or connection
option overrides them, then restores the exact previous value. It changes the
zone only when needed. A query without overrides takes two round trips: read
the session settings, then execute. For overrides, the adapter sets millisecond
server deadlines and uses `adbc_cancel()` as a watchdog through execution and
Arrow fetching. Failed queries discard the connection
before reuse. The libpq connect timeout is ten seconds; a separate network-read
deadline is not exposed.

The hosted Postgres correctness job runs the Arrow adapter unit tests, the conformance battery and
`tests/integration/test_adbc_postgres.py` with the standard `SR_POSTGRES_*`
fixture variables. Exact-type, two-tenant isolation and timeout tests supplement
normalized parity. The conformance loader uses `adbc_ingest` on the production
connection; psycopg is not a runtime or test dependency.

Seed scripts split only at semicolons outside quoted literals, identifiers,
E-string escapes, dollar quotes and comments. Each statement retains its authored
text and comments. An unterminated quote or block comment refuses the entire
script before execution with `INVALID_CONFIG` (`unterminated_sql_script`).
LF and CRLF line endings terminate line comments identically. A bare carriage
return anywhere in a script refuses before execution with `INVALID_CONFIG`
(`bare_carriage_return_sql_script`), naming the SQL source or `post_sql` file.

#### Experimental Snowflake profile

Select `package.connection.kind: snowflake_adbc` to use `SNOWFLAKE_PROFILE`.
This experimental path retains the Snowflake SQL dialect and the existing CLI
and native connectors. Install the Python manager 1.12.0 and PyArrow with
`semantic-rails[snowflake-adbc]` (also included in `[all]`), then install the
target native driver, version **1.14.0**, using an existing `dbc` installation:

```sh
dbc install snowflake=1.14.0
```

The [dbc registry](https://docs.columnar.tech/dbc/guides/finding_drivers/)
distributes the Snowflake driver. The Python manager loads `driver="snowflake"`
from its standard [driver search paths](https://arrow.apache.org/adbc/23/format/driver_manifests.html),
including the operator's `ADBC_DRIVER_PATH` and the user installation directory.
An operator can instead set the runtime environment variable
`SR_SNOWFLAKE_ADBC_DRIVER_PATH` to a shared-library or manifest path. Package
connection options cannot select a driver name, library or manifest; such options
are refused at load with `INVALID_CONFIG`. `dbc` verifies
the downloaded driver signature during installation; do not disable verification.

Account and user may be literals (`account`, `user`) or env-indirected
(`account_env`, `user_env`); password authentication uses either `password_env`
or `password_file`. Optional locators are `database`, `schema`, `warehouse` and
`role`; a named Snowflake connector profile is not used. Key-pair authentication
instead uses `private_key_env` or `private_key_file` containing PEM PKCS #8 text,
with optional `private_key_passphrase_env` or `private_key_passphrase_file`.
Passphrase files preserve whitespace except for one optional trailing LF (`\n`)
or CRLF (`\r\n`); other `*_file` secrets still strip surrounding whitespace.
Both encrypted and unencrypted keys are mapped to the driver's in-memory PKCS #8
options. Exactly one password or
key source is required. Package loading, config reports and guided setup validate
these source options without reading credentials; `connection.name` is refused.
Literal secrets and other authentication modes are refused.
Key-pair mapping is covered with stubs; live key-pair authentication is unqualified.

`use_high_precision` defaults to `"true"`: the driver returns NUMBER columns as
Arrow decimals with precision and scale intact. `"false"` opts into the driver's
integer/float conversion. The [Snowflake driver options](https://arrow.apache.org/adbc/23/driver/snowflake.html#client-options)
describe that conversion and the authentication vocabulary. An optional `query_tag`
is set with escaped session SQL. The adapter binds `?` placeholders without SQL
rewrites; every placeholder must correspond to an authored slot, and each bound
value must match its slot's type. This guard also covers direct prepared calls.

A positive per-query `statement_timeout_ms` takes precedence over the connection
option `statement_timeout_seconds`. With neither, the inherited timeout is preserved.
`ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS` rounds a positive millisecond
limit up to whole seconds; the shared cancellation watchdog uses the original
millisecond limit through execution and fetching. Successful queries restore
the previous timeout and timezone; failures discard the connection. Arrow
conversion preserves Decimal precision and scale and converts aware timestamps
to the query timezone, or the current session zone (falling back to aware UTC
when Python cannot load it). The native driver normalizes TIMESTAMP_TZ instants;
its original per-row offset cannot be recovered. The JSON typed-values contract
emits decimal strings and timestamp strings with an explicit offset. Live
timestamp fidelity, cancellation latency and native-buffer memory bounds remain
qualification work.

The adapter always sets the driver's `max_timestamp_precision` to
`nanoseconds_error_on_overflow`, so timestamps outside Arrow's nanosecond range
fail with `QUERY_EXECUTION_ERROR` instead of wrapping to a different date. Python
temporal results support exact microseconds: nanosecond TIME and TIMESTAMP values
are converted only when exactly representable, including nulls; nonzero
sub-microsecond digits refuse with `RESULT_VALUE_UNSUPPORTED`. This boundary
does not depend on pandas and never truncates temporal precision.

On a credentialed runner, select `SR_SNOWFLAKE_CONNECTION_KIND=snowflake_adbc`
for the existing conformance target. Run its Snowflake battery and the dedicated
exact-type and two-tenant isolation tests:

```sh
SR_SNOWFLAKE_CONNECTION_KIND=snowflake_adbc uv run pytest tests/integration/test_conformance.py tests/integration/test_adbc_snowflake.py -k snowflake -q
```

The conformance target and dedicated tests use `SR_SNOWFLAKE_ACCOUNT`, `SR_SNOWFLAKE_USER`, `SR_SNOWFLAKE_PASSWORD`
and optional `SR_SNOWFLAKE_DATABASE`, `SR_SNOWFLAKE_SCHEMA`,
`SR_SNOWFLAKE_WAREHOUSE`. The isolation test creates only a session-local table,
alternates tenants on identical SQL, checks an injection-shaped attribute and
denies missing attributes. Dedicated tests explicitly skip unless
`SR_SNOWFLAKE_CONNECTION_KIND=snowflake_adbc`, and skip when credentials are absent;
once opted in with credentials, missing drivers and warehouse failures fail the
setup's known-good timestamp query before any expected refusal. The dedicated tests also
check timestamp overflow and sub-microsecond precision refusal. The operator's
`SR_SNOWFLAKE_ADBC_DRIVER_PATH` can override driver discovery. Stub unit tests
prove mapping and lifecycle behavior; they do not establish live qualification.

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
- [ ] `timestamp_type_name`
- [ ] `convert_timezone` — **required if the warehouse supports it**; there
      is no portable spelling, so the base class raises
      `REWRITE_NOT_SUPPORTED` rather than guessing. Skipping this means
      packages using `times.<key>.column_timezone` get a structured error
      on your warehouse instead of SQL that fails at execute time. Existing
      forms: `CONVERT_TIMEZONE(src, tgt, ts)` (Snowflake, Databricks),
      `timezone(tgt, timezone(src, ts))` (DuckDB, Postgres),
      `DATETIME(TIMESTAMP(ts, src), tgt)` (BigQuery),
      `toTimeZone(toDateTime(ts, src), tgt)` (ClickHouse),
      `at_timezone(with_timezone(ts, src), tgt)` (Trino/Athena)
- [ ] `capabilities()` — advertise what the warehouse can and can't do

## 2. Adapter — `semantic_rails/db_parts/redshift.py`

Implement the `WarehouseAdapter` contract. If the driver is DB-API
(PEP 249) — redshift_connector, PyAthena, databricks-sql —
subclass `DbApiAdapter` from `semantic_rails.db_parts.common` and you
only write `_create_connection()` plus the timeout hooks:

```python
from .common import (
    DbApiAdapter,
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
`import_driver`, `require_missing_env`,
`redacted_error_details`, and `rows_from_cursor` from `db_parts.common`, plus
`_clip_rows` / `_limit_timeout_seconds` / `_limit_timeout_milliseconds` from
`db_parts.base`. Translate driver failures with `errors.query_execution_error`;
never include driver messages or stderr in public errors, because they may
contain SQL, result values, or credentials.
DuckDB-family result reads use `materialized_duckdb_result` instead of DB-API
cursor fetches, which can hang on window-function queries. Apply row caps with
the helper's relation limit; calling `fetchmany` would reopen a streamed result.

**Final SQL preparation.** The compiler renders the selected profile, then calls
`SqlDialect.prepare_query` to produce a driver-free `PreparedQuery`: the exact
executable SQL and an immutable mapping from physical result columns to semantic
aliases. Compile, explain, performance analysis, and execution use that statement.
Warehouse syntax and precision rules live in `sql_preparation.py`; add a rule
there when introducing a dialect that needs final preparation. For example,
Redshift's `x / NULLIF(y, 0)` requires the shared `float_nullif_divisions` pass
with `cast_type="DOUBLE PRECISION"` to preserve fractional ratios.

Existing preparation covers PostgreSQL's 63-byte identifiers and floating-point
ratios; BigQuery's backticks and legal field names; Databricks backticks and ratio
precision; Athena temporal comparisons and ratios; and Snowflake ratio precision.
Every rewrite must preserve intended semantics, skip literal contents, and document
the warehouse behavior it compensates for. Reuse the quote-aware helpers in
`sql_preparation.py` instead of implementing another scanner.

`query_prepared(prepared, limits=...)` sends `prepared.sql` unchanged to the driver
and restores result aliases with `restore_column_names`. Session timeout commands
and row limits remain adapter concerns. `DbApiAdapter` supplies both this path and
legacy `query(sql, limits=...)`, which prepares direct SQL calls once. Non-DB-API
adapters needing preparation follow the BigQuery or Snowflake implementation.
The base `WarehouseAdapter.query_prepared` delegates to `query` for existing custom
adapters that have no SQL transformations. Test compile/explain SQL against the
statement captured at the driver boundary, including alias restoration and limits.

**Parameterized statements.** `PreparedQuery.parameters` lists a `ParameterSlot`
for each positional `?` placeholder: the trusted request attribute that supplies
the value and its exact type (`string`, `integer` or `boolean`). The runtime binds
values per request from the host's `TrustedAttributes`; a missing attribute or a
value of another type is denied with `POLICY_DENIED`, never bound as NULL or
coerced. Only an adapter that sets `supports_parameters = True` receives such a
statement, as `query_prepared(prepared, limits=..., parameters=values)`, and it
must send the values to its driver separately from the SQL. Every other adapter,
including the base fallback, denies it before reaching its driver. Never render a
value into SQL text as a fallback. `DuckDBAdapter`, Postgres ADBC and Snowflake
ADBC support binding today; qualify a driver's parameter API with tests before
enabling another.

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
