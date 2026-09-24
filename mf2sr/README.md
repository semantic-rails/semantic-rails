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

## What gets translated

| MetricFlow concept | Semantic Rails analogue |
|---|---|
| `semantic_model` | `model:` block in `models/<name>.yml` |
| `node_relation.alias` | `model.relation` |
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
| `metric.type: ratio` | `kind: ratio`, or `kind: derived` when a side is filtered. The metric's filter applies to both sides; a ratio whose filters can't be kept is skipped |
| `metric.type: cumulative` | `kind: cumulative` (a running total), `kind: rolling` with `window: {unit, value}` for a `window:`, or `kind: period_to_date` with `period:` for a `grain_to_date:`. A filter stays on the aggregate input. The engine adds up each period's value, so the measure must be a sum or a count of the model's own rows |
| `metric.type: derived` | `kind: derived` with Python-AST-parsed arithmetic expression |
| `metric.type: conversion` | Stub `kind: conversion`; author must adapt |

## What gets dropped (with warnings)

| Source shape | Why |
|---|---|
| Entities that appear only as `type: foreign` | Semantic Rails requires every entity to have an owning model. The entity is dropped from the graph; references are stripped from `model.entities` blocks. |
| `semantic_models` whose primary entity is already owned by an earlier model | The model has nothing to claim. Move its measures into the canonical owning model or rename its primary. |
| Measures whose SQL `expr:` contains `CASE`, `LIKE`, `COALESCE`, `NULLIF`, etc. | Semantic Rails' expression parser is a Python AST, not a SQL parser. Rewrite the expression as a `kind: case` AST or push the SQL down into the warehouse model. |
| Filters mf2sr can't translate | A metric keeps its filter when every condition is on one dimension: a boolean dimension, `NOT Dimension(...)`, `IN (...)`, `NOT IN (...)`, `BETWEEN`, or a comparison with a literal (`=`, `!=`, `<>`, `<`, `<=`, `>`, `>=`). Any other condition warns and skips the metric rather than changing its value. This covers `NOT BETWEEN`, `Metric(...)` predicates, `Entity(...) IS NOT NULL`, `entity_path=`, references to dimensions the project doesn't define, and time dimensions, which MetricFlow compares truncated to their grain. |
| Cumulative metrics the engine can't compute | Skipped with a warning when they set both `window` and `grain_to_date`, a window finer than a day, a `grain_to_date` other than week, month, quarter or year, or a measure that doesn't add up across periods: an average, minimum, maximum, median, percentile or distinct count (other than of the model's own key), or a semi-additive measure. |
| Where cumulative values can differ | A warning per metric. Queried at its time dimension's grain, a translated cumulative metric matches MetricFlow. At coarser grains Semantic Rails reports each period's value at its end, which MetricFlow does only with `period_agg: last` (its default is `first`). Period-to-date counts a week toward the month, quarter or year it starts in. Rolling month, quarter and year windows cover whole calendar periods, while MetricFlow's reach back from each day. The engine queries a rolling window only at grains that divide it: day windows at day grain, week windows at day or week grain, month windows at month grain, quarter windows at month or quarter grain, and year windows at month, quarter or year grain. |
| A package calendar | `kind: rolling` metrics are computed over the package calendar, a `kind: time` entity whose table has `date_day`, `week_start`, `month_start`, `quarter_start` and `year_start`. mf2sr doesn't write one, and warns. |
| `derived` inputs with `offset_window` or `offset_to_grain` | Skipped with a warning. Emitting them would compute the input over the same period, so `revenue - revenue_last_month` would be zero. |
| Filters on a `derived` metric or its inputs | The derived metric is skipped with a warning, since its filter or its input's filter would otherwise be dropped. |
| Metrics that use a skipped metric | Skipped too, with a warning; they would fail at query time. |
| `derived` expressions that aren't parseable as Python arithmetic | The metric is emitted as a fallback aggregate over the first input metric with the original formula in the description. |

## Where the output goes

```
<output_dir>/<package_id>/
  package.yml          # schema_version, package id/namespace, warehouse, defaults
  graph.yml            # entities with key/model pointers
  models/<name>.yml    # one per MetricFlow semantic_model that owns an entity
  metrics/<group>.yml  # metrics grouped by the source semantic_model
```

`schema_strict: false` is emitted by default because MetricFlow
measure metadata is too thin to satisfy Semantic Rails' strict
validator (most measures lack a meaningful `value_type:` distinction
to support ratio/derived metric type inference). Flip it to `true`
after reviewing measure value_types and adding governance metadata
(`meta.owner_team`, `meta.review_priority`, `meta.change_risk`).

DuckDB packages emit a placeholder `seed.source` pointing at
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
