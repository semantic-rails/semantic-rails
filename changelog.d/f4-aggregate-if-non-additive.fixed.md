- A query's `aggregate_if` could sum an `additive: false` measure's values by reading its column
  directly (for example `aggregate_if(sum, …, value: daily_visitors)` grouped by repository added
  distinct visitors across days), although the measure itself refused that query. An
  `aggregate_if` whose `value` reads any column such a measure reads, in any letter case and
  whatever else it reads, now follows the measure's rule: summing is refused with
  `ROLLUP_UNSAFE`, naming the measure, unless each output row holds one of its rows, and `avg`,
  `min`, `max` and `count` stay available. This also refuses sums over a non-additive ratio's
  inputs and weighted sums over such a column: declare the input or product as its own measure.
