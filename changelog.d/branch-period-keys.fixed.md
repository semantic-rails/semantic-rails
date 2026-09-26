- A distribution beside a `rolling` or `prior_period` item no longer returns every period twice
  on Postgres and BigQuery. The items are answered separately and joined on the period, which one
  item typed as a date and the other as a timestamp; the join now compares both as the
  warehouse's timestamp, so each period is one row with every item's value.
