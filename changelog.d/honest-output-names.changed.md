- Generated queries name a calculation after what it computes, not after the governed measure
  it starts from. The MCP `build_options` aggregation patch names a non-default aggregation
  `<aggregation>_<measure>` (an average of revenue is `avg_revenue_usd`, not `revenue_usd`).
  A `plan` draft restricted to a population share names its column `<measure>_qualifying`
  (`revenue_usd_qualifying`), and an order on that column follows the new name. Agents that
  read these columns by name see the new names; the numbers are unchanged.
