- A package built with the Architect can answer by month when a fact keeps its date as a key into
  a calendar. `suggest_model` with `project_path` dates the measures of a table that keeps its
  date as one key into a calendar: the draft links the key and lists the calendar's role in each
  measure's `times:`. With several such keys and none the package's model already links (also
  one the calendar mostly lacks, a snapshot's day, or a key named for another date), one beside a date column of the table's own, or one that
  is part of the table's key (a daily inventory keyed by day and product, whose stock a sum by
  month would count once per day), the draft dates none and `time_roles` says the choice is
  yours; a table's own date columns are drafted as before. Only a calendar (`kind: time`, or
  named for a day, date, week, month or calendar with one row per day) dates through a key,
  never another entity's own date, and a calendar's
  unknown-member rows (a NULL or pre-1901 date, a key that is not positive) don't hide it. An
  `INCOMPATIBLE_TEMPORAL_ROLE` for a measure with no time role gives the line that dates it
  (`details.declare`), and validation and `promotion_check` warn `MEASURE_WITHOUT_TIME_ROLE` for
  such a measure whose rows reach a clock. See [suggest_model](docs/ARCHITECT_MCP.md).
