# Support-Label Rubric

`shared/scripts/apply_rubric.py` generates every support label in this pack from each layer's
committed artifacts. Every layer, Semantic Rails included, gets the same rules. Nobody assigns
labels by hand, and the runners this pack re-runs record only whether a question executed.
Each label is published with the rule that fired and the evidence behind it in
[`results/rubric/labels.json`](results/rubric/labels.json).

## Labels

The rules are checked in order, and the first one that applies sets the label. The first two
apply only to the frozen-model questions (q17-q24, below).

| Label | Rule |
| --- | --- |
| `not_assessed` | Frozen-model questions only: the layer isn't listed in [`frozen_model.yml`](frozen_model.yml), because this pack can't run it on them (Snowflake Semantic Views, a stale capture). |
| `requires_model_change` | Frozen-model questions only: the layer's documented query-time interface can't express the question with its model unchanged. `frozen_model.yml` gives the reason and the documentation, or the error the layer returned. |
| `unsupported` | The layer didn't execute the question. |
| `precomputed` | The answer reads a cross-row rollup column that the question declares in `bypass_columns`, either in the executed SQL or in SQL that it depends on. |
| `workaround` | The answer depends on SQL written by hand for this pack: a derived table, a SQL-defined source or cube, or a statement outside the layer's semantic interface. |
| `native` | None of the above: the answer uses only the layer's own semantic constructs (measures, dimensions, joins, metrics and member filters). |

## Frozen-Model Questions

q01-q16 were answered with models written for them: every layer's model was authored with those
16 questions in view, so most answers read a metric, measure, named query or source written for
that question. Their labels say where each answer's logic lives. They don't say whether a layer
could answer a question its model wasn't written for.

q17-q24 (`scope_level: variant` in `questions.yml`) test that. Each changes one parameter of a
metric that q01-q16 already use: a conversion window, a rolling window, a period offset, a
per-metric filter, an aggregation, or a threshold. No layer's model defines the variant. Every
layer answers them with its model exactly as written for q01-q16, through its documented
query-time interface only, and the same rules then label each answer:

- **The model stays frozen.** `frozen_model.yml` lists each layer's model files and pins their
  sha256. The rubric refuses to label anything if a model differs from its pin, so a variant
  can't be answered by editing the model.
- **Only the query-time interface.** MetricFlow: `mf query` (metrics, group-by, `--where`
  including metric filters, time bounds). Cube: REST queries, and SQL API queries sent to
  `/v1/cubesql`, which can wrap a Cube query in SQL. Malloy: queries in their own files that
  import the model, including filtered and ad hoc aggregates, calculations and query-level source
  extensions. KtX: ktx-sl semantic queries, including inline measure expressions. Semantic Rails:
  the Query API. Each layer's query for a variant is in its `queries/` folder.
- **`requires_model_change`** is declared, not detected: `frozen_model.yml` gives the exact reason
  and a documentation link for each one, and the competitor-advocate reviews check that no layer
  was denied a query-time feature it has. The rubric fails if a layer both declares one and
  executes it.
- **An answer that runs is labeled like any other.** A Cube SQL API query that wraps a Cube query
  in a derived table or a window function is SQL written by hand, so it is `workaround`, like SQL
  outside `SEMANTIC_VIEW(...)`; `frozen_model.yml` says why for each one, with a documentation
  link, and the rubric refuses a frozen-model `workaround` without one. A Malloy query that
  extends a source with its own join, or a KtX or Semantic Rails query that composes an
  aggregate inline, uses the layer's semantic constructs and is `native`.

The published matrix gives each layer's count as "answered with the model frozen": the
frozen-model questions it answered (`native`, `workaround` or `precomputed`), out of 8.

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
| Semantic Rails | Any relation pipeline or aggregate relation in the package, and any model whose `relation` isn't one shared view or that declares `relation_ref` or `variants`. The rubric reads the package the way the engine does, so it checks models declared in `package.yml` or `models/`, and relations declared in `package.yml`, `relations.yml` or `relations/`. It doesn't resolve which models a metric reads, so one such object anywhere in the package counts against every answer. |
| MetricFlow | Any dbt model that the executed SQL reads and that isn't a passthrough. The time spine is exempt because MetricFlow requires one. |
| Cube | Any cube used by the query's members (measures, dimensions, time dimensions and filters) that is defined with `sql:` and isn't a passthrough. `sql_table:` cubes and the `sql` of a declared join are Cube's own syntax. An SQL API query (`queries/*.sql`) that reads a derived table or uses a window function. |
| Malloy | Any `jaffle.sql(...)` block in the model or the question's own query file, as a source or a join declared inline, whose whole SQL appears in the executed SQL as a parenthesized derived table and isn't a passthrough. Malloy compiles that SQL into the query verbatim wherever the query reads it, whether directly, through a join or alias, or under an `extend`, so a declared join the query doesn't read doesn't count. |
| KtX | Any source used by the query's fields, inline measure expressions included, that is defined with `sql:` and isn't a passthrough. |
| Snowflake Semantic Views | A captured statement that doesn't go through `SEMANTIC_VIEW(...)`. |

**Fail closed.** A detector that finds nothing to check stops the rubric instead of labeling the
answer `native`. That means no executed SQL for an executed answer, no relations in MetricFlow's
executed SQL, no cubes, KtX sources or Semantic Rails models, or no named Malloy query.

## What The Labels Don't Say

- **q01-q16 were answered with metrics written for them, in every layer.** The pack's authors
  wrote every layer's model, Semantic Rails included, with those 16 questions in view, and most
  answers read a metric, measure, named query or source written for that question. So a
  `native` label on q01-q16 doesn't show that a layer could answer a question its model wasn't
  written for. The frozen-model questions (q17-q24) test that, with every model unchanged.
- A label describes how this pack models a layer today, not what the layer can do. Snowflake
  Semantic Views ships range joins that this pack hasn't modeled (see the README), and the rubric
  checks where an answer's logic lives, not whether the model is idiomatic.
- The Semantic-Rails-targeted questions (q08-q16) were chosen to exercise Semantic Rails
  features. The Semantic Rails authors wrote every layer's models. Those questions are a
  capability showcase, not a ranking.
- Detection is textual. It matches column names and inspects model files, so a bypass column
  read under another name would go unnoticed. The tests pin the current labels
  (`tests/semantic_rails/test_comparison_pack_scripts.py`), so any change to a label shows up
  in review.
- Detection doesn't parse member, entity or join expressions. The methodology keeps them
  row-level, and review checks that no derived table hides in one. A layer's own
  derived-query constructs, such as Malloy query-derived sources, Cube subquery dimensions
  and multi-stage measures, and MetricFlow conversion metrics and metric filters, are
  semantic constructs, not hand-written SQL.
- Snowflake's committed `summary.json` predates the rubric and still carries hand-assigned
  labels. The rubric reads only whether each question executed.
