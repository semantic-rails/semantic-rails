- `plan` keeps the time grain a question asks for on a metric that declares no
  `temporal_role`, reading it on the clock its measures share: "total amount by month" on a
  new package (`semantic-rails init`, or an Architect scaffold over a dbt warehouse) is a monthly
  series, not one total. A metric that no one clock dates, such as a ratio of two models' counts,
  is held with `PLAN_UNMATCHED_TERMS` naming the grain instead of answering one total as ready.
- `plan` reads a series' grain without the words the draft already spends: a filter value it
  keeps ("total amount for monthly billing period" is one total, not a monthly series) or the
  name of what it selects ("revenue per day by month" is monthly, not daily). This also fixes
  measures and metrics that declare a clock. A derived metric whose parts share no one clock is
  held instead of drafted on one part's clock, and the hold's hint says each part keeps its own.
