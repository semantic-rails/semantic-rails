- `kind: fact` models are removed: give such a model an entity keyed by its time column, or
  leave it row grain. The sample package's daily and monthly metric series are keyed by
  `date_day` and `month_start`, with the same answers.
