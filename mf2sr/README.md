# mf2sr — MetricFlow → Semantic Rails translator

Deterministically turn a MetricFlow project into a Semantic Rails package.

The translator reads either a directory of MetricFlow YAML files
(`semantic_model:`, `metric:`, `project_configuration:`) or a parsed
`semantic_manifest.json` artifact, and emits a complete Semantic Rails
package directory that loads under `semantic-rails parse-config`.

No LLM is involved — every mapping is rule-based, and any shape the
translator cannot round-trip cleanly is logged as a warning so a human
reviewer can address it.

## Quick start

```bash
# From the semantic-rails repo root.

# A: directory of MetricFlow YAML files
python -m mf2sr \
  --source /path/to/metricflow/yamls \
  --output configs/semantic_rails \
  --package-id shop \
  --warehouse duckdb

# B: parsed semantic_manifest.json (e.g. dbt target output)
python -m mf2sr \
  --source path/to/target/semantic_manifest.json \
  --output configs/semantic_rails \
  --package-id shop \
  --warehouse snowflake
```

The CLI prints emitted model / metric counts and a list of warnings.
Pass `--strict` to exit non-zero when any warning fires.
The package destination must be new or empty. mf2sr refuses a nonempty
destination before writing because it cannot distinguish earlier generated
files from authored files; choose a fresh output path for each translation.

## What gets translated

| MetricFlow concept | Semantic Rails analogue |
|---|---|
| `semantic_model` | `model:` block in `models/<name>.yml` |
| `node_relation` | `model.relation`: the alias, or with `--schema-strict` the schema-qualified name |
| `entities[*].type: primary/unique` | Graph entity in `graph.yml` with `key:` and `model:` |
| `entities[*].type: foreign` | FK entry in `model.entities` (if the entity has an owner) |
| `primary_entity:` (no explicit primary) | Synthetic primary with key `<name>_id` |
| `dimensions[*].type: time` | `model.times.<role>` with `class: event_time` |
| `dimensions[*].type: categorical/boolean/integer` | `model.dimensions` |
| `defaults.agg_time_dimension` | Marks the matching `times:` entry as `default: true` |
| `measures[*].agg: sum/avg/min/max/median/percentile` | `kind: aggregate` with `default_agg:` |
| `measures[*].agg: count_distinct` | `kind: entity_count` when the column resolves to a graph entity; else `SUM(CASE WHEN col IS NOT NULL THEN 1 ELSE 0 END)` with a warning |
| `measures[*].agg: count` | Same fallback as `count_distinct` when no entity matches |
| `measures[*].agg: sum_boolean` | `default_agg: sum` over a `kind: case` AST that returns 1/0 |
| `measures[*].expr: "1"` | `kind: entity_count` over the model's primary entity |
| `metric.type: simple` | `kind: aggregate` over the named measure |
| `metric.type: simple` + `filter:` | `kind: aggregate` with `expression: {kind: aggregate, measure, aggregation, filter: {all: [{field, op, value}]}}`. The metric's filter and its measure input's filter are ANDed, and each `entity__dimension` reference becomes that dimension's id |
| `metric.type: ratio` | `kind: ratio`, or `kind: derived` when a side is filtered. The metric's filter applies to both sides; a ratio whose filters can't be kept is skipped. An unfiltered side retains its explicit source metric definition. |
| `metric.type: cumulative` | `kind: cumulative` (a running total), `kind: rolling` with `window: {unit, value}` for a `window:`, or `kind: period_to_date` with `period:` for a `grain_to_date:`. A filter stays on the aggregate input. The engine adds up each period's value, so the measure must be a sum or a count of the model's own rows |
| `metric.type: derived` | `kind: derived` with Python-AST-parsed arithmetic expression |
| `metric.type: conversion` | Stub `kind: conversion`; author must adapt |

## What gets dropped (with warnings)

| Source shape | Why |
|---|---|
| Entities that appear only as `type: foreign` | Semantic Rails requires every entity to have an owning model. The entity is dropped from the graph; references are stripped from `model.entities` blocks. |
| `semantic_models` whose primary entity is already owned by an earlier model | The model has nothing to claim. Move its measures into the canonical owning model or rename its primary. |
| Measures whose SQL `expr:` contains `CASE`, `LIKE`, `COALESCE`, `NULLIF`, etc. | Semantic Rails' expression parser is a Python AST, not a SQL parser. Rewrite the expression as a `kind: case` AST or push the SQL down into the warehouse model. |
| Filters mf2sr can't translate | A metric keeps its filter when every condition is on one dimension: a boolean dimension, `NOT Dimension(...)`, `IN (...)`, `NOT IN (...)`, `BETWEEN`, or a comparison with a single-quoted string or numeric literal (`=`, `!=`, `<>`, `<`, `<=`, `>`, `>=`). Any other condition warns and skips the metric rather than changing its value. This includes double-quoted SQL identifiers, `NOT BETWEEN`, `Metric(...)` predicates, `Entity(...) IS NOT NULL`, `entity_path=`, references to dimensions the project doesn't define, and time dimensions, which MetricFlow compares truncated to their grain. |
| Cumulative metrics the engine can't compute | Skipped with a warning when they set both `window` and `grain_to_date`, a window finer than a day, a `grain_to_date` other than week, month, quarter or year, or a measure that doesn't add up across periods: an average, minimum, maximum, median, percentile or distinct count (other than of the model's own key), or a semi-additive measure. |
| Where cumulative values can differ | A warning per metric. Matching MetricFlow at the metric's time grain depends on a supported window/grain combination and the period-aggregation and calendar-boundary semantics below; translation alone does not guarantee parity. At coarser grains Semantic Rails reports each period's value at its end, which MetricFlow does only with `period_agg: last` (its default is `first`). Period-to-date counts a week toward the month, quarter or year it starts in. Rolling month, quarter and year windows cover whole calendar periods, while MetricFlow's reach back from each day. The engine queries a rolling window only at grains that divide it: day windows at day grain, week windows at day or week grain, month windows at month grain, quarter windows at month or quarter grain, and year windows at month, quarter or year grain. |
| A package calendar | `kind: rolling` metrics are computed over the package calendar, a `kind: time` entity whose table has `date_day`, `week_start`, `month_start`, `quarter_start` and `year_start`. mf2sr doesn't write one, and warns. |
| `derived` inputs with `offset_window` or `offset_to_grain` | Skipped with a warning. Emitting them would compute the input over the same period, so `revenue - revenue_last_month` would be zero. |
| Filters on a `derived` metric or its inputs | The derived metric is skipped with a warning, since its filter or its input's filter would otherwise be dropped. |
| Ratios that filter an explicit source metric they can't reproduce | Skipped with a warning. A source metric that is filtered, non-simple, or named differently from its underlying measure cannot safely be flattened to a filtered measure aggregate. |
| Metrics that use a skipped metric | Skipped too, with a warning. Explicit source metrics take precedence over same-named measures, including in ratios and transitive dependents. |
| `derived` expressions that aren't parseable as Python arithmetic | The metric is emitted as a fallback aggregate over the first input metric with the original formula in the description. |

## Where the output goes

```
<output_dir>/<package_id>/
  package.yml          # schema_version, package id/namespace, warehouse, defaults
  graph.yml            # entities with key/model pointers
  models/<name>.yml    # one per MetricFlow semantic_model that owns an entity
  metrics/<group>.yml  # metrics grouped by the source semantic_model
```

By default the package is `schema_strict: false` and each model's `relation`
is the bare dbt alias (`fct_orders`), so a project that needs review still loads.

`--schema-strict` (`translate(schema_strict=True)`, also on
`semantic-rails import --from metricflow`) writes a `schema_strict: true`
package over the tables dbt built:

- Each relation keeps the schema from its `node_relation` (`main_marts.fct_orders`),
  named the way `import_dbt_project` names dbt relations: the database leads only
  when it differs from the one most models use, and dbt-duckdb's default `main`
  schema is left out. dbt's `target/semantic_manifest.json` records these for the
  target it was built with, so generate it for the target the package will read
  (for example `dbt parse --target prod`). A MetricFlow YAML directory
  (`model: ref('fct_orders')`) records none, so its relations stay bare, with a
  warning.
- A DuckDB package gets `seed: {kind: external}`: it reads the database dbt builds,
  which it never rebuilds. Point `--default-db` at that file, inside the package.
- The output is parse-checked, and each error is a `parse:` warning, so `--strict`
  fails the run. mf2sr writes a `connection` block for DuckDB and Snowflake only;
  add one for another warehouse before the package parses.

Without `--schema-strict`, DuckDB packages emit a placeholder `seed.source` pointing at
`data/seed_<package_id>.sql` that the author must create. Snowflake
packages emit a `connection.kind: snowflake_native` block reading
credentials from environment variables.

## Programmatic use

```python
from pathlib import Path
from mf2sr import translate

report = translate(
    Path("/path/to/metricflow/yamls"),
    Path("configs/semantic_rails"),
    package_id="shop",
    warehouse="duckdb",
)

print(f"Wrote {report.package_dir}")
print(f"Models: {report.models_emitted}")
print(f"Warnings: {len(report.warnings)}")
```

## Validation

```bash
# After translation, confirm the package loads.
uv run semantic-rails parse-config --path <output_dir>/<package_id>

# Run the translator's own test suite.
uv run pytest tests/mf2sr -q
```
