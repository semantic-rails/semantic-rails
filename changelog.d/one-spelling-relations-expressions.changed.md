- Breaking: relations, expressions, graph cardinality, the route policy and spec files each
  accept one spelling, and loading refuses the others with `INVALID_CONFIG` (or
  `INVALID_EXPRESSION_*` for a query) and a hint naming the current form:
  - Expression `kind: binary` is `kind: arithmetic`, `kind: measure_ref` is `kind: measure`,
    conversion `matching:` is `matching_mode:`, and `in`/`not_in` `left:` is `expr:`, in
    packages and in Query IR v1, whose schema drops the three aliases.
    `semantic-rails project upgrade` rewrites them (rule `expression-arithmetic`).
  - `relations:` is a map keyed by relation, and each relation's `steps:` is a list of one-key
    steps such as `{source: orders}`. A list of relations, relation-level `source:` and
    `date_spine:`, `cte:` (write `output_name:`), `output_columns:` (write `columns:`),
    `{kind: ..., config: ...}` and bare-string steps, and `unnest` (write `explode`) are
    refused, as is any key a relation or step doesn't read.
  - A graph relationship's `cardinality:` is `many_to_one`, `one_to_many`, `one_to_one` or
    `many_to_many`; `N:1`, `1:N`, `1:1`, `M:N` and other spellings are refused.
  - `path_policy:` and `path_preferences:` are authored under `graph:`; the top-level keys
    are refused, and `record_route_decision` writes rows only under `graph:`.
  - A file under `models/` holds one model under `model:`, and a file under `relations/`,
    `metrics/` or `segments/` holds a `relations:`, `metrics:` or `segments:` map; a
    `models:` map, a `relation:`, `metric:` or `segment:` wrapper, or a bare spec is refused.
