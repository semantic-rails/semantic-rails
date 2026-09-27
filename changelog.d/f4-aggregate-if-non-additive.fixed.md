- A query's `aggregate_if` could sum an `additive: false` measure's values by reading its column
  directly (for example `aggregate_if(sum, …, value: daily_visitors)` grouped by repository added
  distinct visitors across days), although the measure itself refused that query. An
  `aggregate_if` whose `value` reads every column such a measure reads (its column, or all the
  inputs of a computed one, however wrapped, in any letter case) now follows the measure's rule:
  summing is refused with `ROLLUP_UNSAFE` unless each output row holds one of its rows, and
  `avg`, `min`, `max` and `count` stay available.
