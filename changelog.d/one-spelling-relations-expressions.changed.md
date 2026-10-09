- Breaking: relations, expressions, graph cardinality, the route policy and spec files each
  accept one spelling, and the others are refused with `INVALID_CONFIG` (or
  `INVALID_EXPRESSION_*` for an expression) and a hint naming the current form. Loading
  refuses them, except an expression in a relation step, which refuses when the relation
  compiles:
  - Expression `kind: binary` is `kind: arithmetic`, `kind: measure_ref` is `kind: measure`,
    conversion `matching:` is `matching_mode:`, and `in`/`not_in` `left:` is `expr:`, in
    packages and in Query IR v1, whose schema drops the three aliases.
    `semantic-rails project upgrade` rewrites them (rule `expression-arithmetic`).
  - `relations:` is a map keyed by relation, and each relation's `steps:` is a list of one-key
    steps such as `{source: orders}`. A list of relations, relation-level `source:` and
    `date_spine:`, `cte:` (write `output_name:`), `output_columns:` (write `columns:`),
    `{kind: ..., config: ...}` and bare-string steps, and `unnest` (write `explode`) are
    refused, as is any key a relation or step doesn't read.
  - Each key a relation step reads has one spelling, in the step and in each mapping nested in
    it, and loading refuses a second one with a hint naming the key the step reads:
    `relation:` (not `table:`, `name:`, or `source:` in a `union_all` branch), `columns:`,
    `predicates:` and `branches:` (not `value:`, `select:` or `where:`), `dimensions:` and
    `aggregates:` (not `group_by:` or `measures:`), an aggregate's or window's `function:` and
    `expr:` (not `agg:`, `kind:` or `expression:`), `require_pre_aggregate:`, `date_lag`
    `max:`, `windows:`, `order_by` `expr:` (not `column:`), `as:` (not `alias:`),
    `date_spine` `column:`, `spine:`, `base:` and `attributed:` (not `base_relation:`,
    `attributed_relation:`, or a key's `left:` and `right:`), and `lookback` `value:` (not
    `max:`). `{source: <relation>}`, `{where: [...]}` and `{union_all: [...]}` stay as short
    forms, and a key starting with `_` beside a step's kind is an annotation.
  - A predicate row (a row with `field:`) in a `where` step, or in the `where:` of a
    `semi_join`, `anti_join` or `exclude` step, takes `field`, `op` and `value` only; any
    other key, such as `operator:`, is refused instead of filtering with `op` defaulted to `=`.
  - `semantic-rails project upgrade` (rule `expression-arithmetic`) also rewrites the retired
    expression spellings in a measure's `expr:` and in relation steps.
  - A graph relationship's `cardinality:` is `many_to_one`, `one_to_many`, `one_to_one` or
    `many_to_many`; `N:1`, `1:N`, `1:1`, `M:N` and other spellings are refused.
  - `path_policy:` and `path_preferences:` are authored under `graph:`; the top-level keys
    are refused, and `record_route_decision` writes rows only under `graph:`.
  - A file under `models/` holds one model under `model:`, and a file under `relations/`,
    `metrics/` or `segments/` holds a `relations:`, `metrics:` or `segments:` map; a
    `models:` map, a `relation:`, `metric:` or `segment:` wrapper, or a bare spec is refused.
