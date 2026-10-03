- Count each series of a stock measure once when grouped by an attribute that changes
  within the period: the period's snapshot is chosen first and the attribute read from it,
  so an account that moves from one plan to another mid-week appears only under the plan
  it ends the week on, and the grouped rows add up to the ungrouped total. Grouping by the
  stock's own clock or a calendar dimension still answers each of those periods separately.
