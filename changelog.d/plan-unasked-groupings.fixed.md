- `plan` is no longer ready when the draft adds a grouping the question never asks for: "food
  revenue vs drink revenue by store and customer type" split by month is held with
  `why.code="PLAN_UNASKED_GROUPING"` and the month in `why.details.unasked_groupings`. A grain
  traces to the question's words outside its windows ("by month", "monthly", "over time"), to
  the caller's `partial_query`, or to a window that fits in one bucket; a dimension traces to a
  grouping the question asks for ("by store", "which 5 stores", "per store", "for each store"),
  to the caller's group_by, or to a filter on values the question names. So a comparison, a
  year-over-year shift or a qualified ranking that buckets by month, and a window of several
  periods split into them ("revenue last 7 days" by day, "revenue in 2016 and 2017" by year),
  are held until the question names the grain. The check only holds a plan: the draft and every
  other plan are unchanged.
- A ranking split by a period the question names ("top 3 stores by revenue at month level") is
  no longer ready with the top 3 store-months. It is held with
  `why.code="PLAN_RANKING_PERIOD_AMBIGUOUS"`, and `why.details.clarification` offers the top 3
  overall (with a breakdown query for them by month) and the top 3 in each month, each with
  Query IR to run. Plan offers them only when the ranked noun reads every grouping, each value
  is a plain measure or metric that sums to one total over a window, and each query validates.
  Any other ranking that keeps more than the ranked entity ("which 3 stores have the highest
  revenue by customer type" keeps the top 3 store and customer type pairs, a running total, a
  ranked month) is held with the same code and no runnable option.
- The listed-grouping check reads a window inside the list as a comma, so "repair cost by
  incident name, last month and incident" grouped by the incident name alone is held instead of
  adding two incidents that share a name into one row.
