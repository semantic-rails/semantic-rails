- Generated queries name a calculation after what it computes, not after the governed measure
  it starts from. The build-options aggregation patch (`POST /api/v1/build-options` and the
  `build-options` CLI command) names a non-default aggregation `<aggregation>_<measure>` (an
  average of revenue is `avg_revenue_usd`, the median-percentile option `p50_revenue_usd`), and
  a `plan` draft restricted to a population share names its column `<measure>_qualifying`. A
  generated name never takes a name a governed metric or measure answers to (jaffle_shop's
  `median_item_revenue_usd` is a governed daily-snapshot median, so the median option is
  `item_revenue_usd_median`), and an `order_by` or `having` on the old column follows the new
  name. Agents that read these columns by name see the new names; the numbers are unchanged.
