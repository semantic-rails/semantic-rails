# Apache Ossie export and import

`semantic-rails export --format ossie` writes a package as an
[Apache Ossie](https://github.com/apache/ossie) 0.1.1 semantic model, the spec's only tagged
version:

```bash
uv run semantic-rails export --format ossie --package jaffle_shop --output dist/ossie
```

It writes two files and prints a JSON report with the counts and warnings:

- `<package-id>.ossie.yaml`: the Ossie document. It passes the spec's own validator
  (`validation/validate.py` at tag `osi-0.1.1-rc1`), which the tests run on the repository's
  example packages.
- `<package-id>.semantic_rails.json`: the sidecar, holding everything the document can't carry.
  Ossie 0.1.1 accepts `custom_extensions` only from six named vendors, so Semantic Rails data can't
  ride inside the document itself.

`semantic-rails import --from ossie` reads a document back ([Import](#import)). Writing Ossie 0.2 is
not supported yet.

## Mapping

| Semantic Rails | Ossie 0.1.1 |
| --- | --- |
| Package id and description | Semantic model `name` and `description` |
| Entity | Dataset: `source` is the entity's table, `primary_key` its key columns (composite keys stay composite) |
| Dimension | Field with `dimension.is_time` (true for date and timestamp columns and temporal-role dimensions) |
| Measure | Field without `dimension`, whose expression is the measure's row-level SQL |
| N:1 or 1:1 relationship marked safe; 1:N reversed | Relationship from the many side to the one side |
| Aggregate, ratio and derived metric | Metric: aggregate SQL over `dataset.field`, dividing by `NULLIF(denominator, 0)` as the engine does |
| `label`, `description`, `aliases` | `label` (fields only), `description`, `ai_context.synonyms` |

Names are the object id without its type prefix, with other characters replaced by `_`, so
`metric.sales.aov_usd` becomes `sales_aov_usd`. The sidecar's `names` map each Ossie name back to
its Semantic Rails id, and its `package` block carries the package id and namespace that identify
metrics in the [metric portability contract](CONTRACTS.md#metric-portability-for-bi-consumers).
Expressions use the `SNOWFLAKE` or `DATABRICKS` dialect for those warehouses and `ANSI_SQL`
otherwise. Functions an author wrote pass through unchanged, and date arithmetic uses the package
warehouse's syntax, so an `ANSI_SQL` expression can still contain engine-specific SQL.

## What stays in the sidecar

Each construct the document can't carry gets one warning with a count, and the sidecar lists the
affected ids. Nothing is dropped silently.

- **Left out of the document, kept whole in the sidecar.** Anything Ossie 0.1.1 can't state
  faithfully is omitted rather than approximated, so an Ossie consumer never computes a different
  number under a Semantic Rails name:
  - temporal roles, value domains, segments, semantic policies and caveats, aggregate relations,
    relation pipelines, path preferences and path policy;
  - cumulative, rolling, prior-period, period-to-date, conversion and semi-additive metrics;
    filtered or windowed metrics; metrics that pin a clock; metrics using an aggregation other
    than `SUM`, `COUNT`, `COUNT DISTINCT`, `AVG`, `MIN` or `MAX`; metrics over a semi-additive
    measure or one rolled up to another entity; and metrics built on anything left out;
  - entities built by a relation pipeline (their table is a query the engine builds, not one a
    consumer can read), with their dimensions, measures and relationships;
  - measures read from another relation or entity;
  - M:N, time-valid and unsafe relationships, N:1 or 1:1 relationships that need a rewrite, and
    relationships whose `allowed_directions` exclude the many-to-one direction.
- **Exported, with extra attributes in the sidecar.** For example a dimension's data type and
  semantic kind, a measure's default aggregation and accumulation, or a metric's temporal role.
  The sidecar's `expressions` also keep the exact expression behind each exported measure and
  metric, which the SQL alone can't give back.

Semantic policies also get a separate `policy enforcement` warning: Ossie consumers don't read
the sidecar and won't enforce them, so anyone given the document sees every exported object.

The export covers the semantic model only. Deployment settings (`connection`, `seed`,
`default_db`) and the package's examples and tests are not part of it.

## Import

```bash
uv run semantic-rails import --from ossie --source dist/ossie/jaffle_shop.ossie.yaml \
  --output dist/imported --package-id jaffle_shop
```

It reads an Ossie 0.1.x document (the first model in `semantic_model`) or a 0.2 document (one
model at the root), writes the package to `<output>/<package-id>/`, and prints a JSON report
with the counts and warnings. Like the export, it never drops anything silently: every construct
it skips or fills with a default gets one warning with the affected names.

- **With the sidecar** (`<name>.semantic_rails.json` beside the document, as the export writes
  it), every object comes back exactly: the document supplies what it carries and the sidecar
  the rest, including the objects the export left out. The import then exports what it wrote and
  compares that with the document and sidecar it read. The report says `round_trip: exact`, or
  lists each difference, for example a metric whose SQL was edited after the export.
- **Without it**, the import keeps what the document states and uses defaults for the rest:
  - datasets with a table `source` and a `primary_key` become entities;
  - fields that name a column become dimensions, typed as categories, or as timestamps when
    `dimension.is_time` or the 0.2 `datatype` says so. Time fields get a temporal role with day
    to year grains;
  - fields without `dimension` become measures when their SQL is a column or simple arithmetic,
    aggregated the way the metrics use them, and counted distinct if any metric does;
  - relationships become many-to-one joins;
  - metrics are imported when their SQL is the aggregate SQL the export writes:
    - `SUM`, `AVG`, `MIN`, `MAX` or `COUNT(DISTINCT ...)` over `dataset.field`;
    - numbers, parentheses, and `+`, `-` and `*`;
    - division by `NULLIF(denominator, 0)`;
    - `COALESCE(x, 0)` on both sides of `+` or `-`.

  Anything else is skipped with a warning: computed dimensions, other SQL, datasets defined by a
  query, `unique_keys`, `custom_extensions`, and `ai_context` beyond `synonyms`.

The imported package reads data another tool built: `--default-db` names the DuckDB file
(default `data/<package-id>.duckdb`, with `seed: {kind: external}`). A document written for
Snowflake gets a `snowflake_cli` connection named after the package. `--warehouse`,
`--description` and `--schema-strict` apply to `--from metricflow` only.
