# Support-Label Rubric

`shared/scripts/apply_rubric.py` generates every support label in this pack from each layer's
committed artifacts. Every layer, Semantic Rails included, gets the same rules. Nobody assigns
labels by hand, and the runners this pack re-runs record only whether a question executed.
Each label is published
with the rule that fired and the evidence behind it in
[`results/rubric/labels.json`](results/rubric/labels.json).

## Labels

The rules are checked in order, and the first one that applies sets the label.

| Label | Rule |
| --- | --- |
| `unsupported` | The layer didn't execute the question. |
| `precomputed` | The answer reads a cross-row rollup column that the question declares in `bypass_columns`, either in the executed SQL or in SQL that it depends on. |
| `workaround` | The answer depends on SQL written by hand for this pack: a derived table, a SQL-defined source or cube, or a statement outside the layer's semantic interface. |
| `native` | None of the above: the answer uses only the layer's own semantic constructs (measures, dimensions, joins, metrics and member filters). |

## How Each Rule Is Detected

**Executed.** A question counts as executed when the layer's `summary.json` lists it with a
status other than `unsupported`, and the layer's `unsupported.json` doesn't list it. A question
missing from both is `unsupported`.

**Bypass columns.** A question declares `bypass_columns` when the shared data already holds the
aggregate that the question asks each layer to compute:

- q11 declares `lifetime_order_count`.
- q12 declares `lifetime_spend_cents`.

The rubric searches the executed SQL for those column names, along with the SQL of any
hand-written object that the answer depends on.

q06 declares none. Every layer counts first orders with the seed's `is_new_customer_order`
flag, which the answer key derives independently to check the flag itself. The flag marks an
order's place in its customer's sequence rather than aggregating across rows. q06 is also a
shared question that every layer answers the same way, so every layer would get the same label
either way.

**Hand-written SQL.** A trivial passthrough of one shared view (`select * from comparison_x`)
isn't hand-written logic. Everything the rubric does count is listed here:

| Layer | Counted as hand-written SQL |
| --- | --- |
| Semantic Rails | Any relation pipeline or aggregate relation in the package, or any model whose `relation` isn't one shared view. The rubric reads the package the way the engine does, so models declared in `package.yml`, `models/` or relation files all count. It doesn't resolve which models a metric reads, so one such object anywhere in the package counts against every answer. |
| MetricFlow | Any dbt model that the executed SQL reads and that isn't a passthrough. The time spine is exempt because MetricFlow requires one. |
| Cube | Any cube used by the query's members (measures, dimensions, time dimensions and filters) that is defined with `sql:` and isn't a passthrough. `sql_table:` cubes and the `sql` of a declared join are Cube's own syntax. |
| Malloy | Any `jaffle.sql(...)` source that the query reads, directly or through a join, including an aliased join (`join_one: alias is source`). |
| KtX | Any source used by the query's fields that is defined with `sql:` and isn't a passthrough. |
| Snowflake Semantic Views | A captured statement that doesn't go through `SEMANTIC_VIEW(...)`. |

**Fail closed.** A detector that finds nothing to check stops the rubric instead of labeling the
answer `native`. That means no relations in MetricFlow's executed SQL, no cubes, KtX sources or
Semantic Rails models, or no named Malloy query.

## What The Labels Don't Say

- A label describes how this pack models a layer today, not what the layer can do. Several
  layers ship features that this pack hasn't modeled yet (see the README), and the rubric
  checks where an answer's logic lives, not whether the model is idiomatic.
- The Semantic-Rails-targeted questions (q08-q16) were chosen to exercise Semantic Rails
  features. The Semantic Rails authors wrote every layer's models. Those questions are a
  capability showcase, not a ranking.
- Detection is textual. It matches column names and inspects model files, so a bypass column
  read under another name would go unnoticed. The tests pin the current labels
  (`tests/semantic_rails/test_comparison_pack_scripts.py`), so any change to a label shows up
  in review.
- Snowflake's committed `summary.json` predates the rubric and still carries hand-assigned
  labels. The rubric reads only whether each question executed. Cube's pinned capture
  (`results/cube/summary.json`) carries them too. The rubric reads the replay's summary
  instead.
