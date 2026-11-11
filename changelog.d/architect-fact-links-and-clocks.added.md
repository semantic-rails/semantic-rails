- A package built with the Architect can answer by month and across two facts. `suggest_links`
  proposes a link on a key of two or more columns whose columns are named as the key's columns
  (on a table with no other key), or all of them with one common prefix or suffix (a return and
  the sale line it names by
  `sale_order_id, sale_line_no`), saying the target key is never null and never repeated, the
  columns matched and how many rows find a match; a target whose columns repeat together is
  never drafted, and a tuple that is not the table's own key is drafted `many_to_one`.
  `suggest_model` with `project_path` dates the measures of a table that keeps its date as one
  key into a calendar: the draft links the key and lists the calendar's role in each measure's
  `times:`. With several such keys (also one the calendar mostly lacks, or a snapshot's day), or
  one beside a date column of the table's own, the draft dates none and `time_roles` says the
  choice is yours; a table's own date columns are drafted as before. Only a calendar (`kind: time`, or named for a day, date, week, month or calendar
  with one row per day) dates through a key, never another entity's own date, and a calendar's
  unknown-member rows (a NULL or pre-1901 date, a key that is not positive) don't hide it. Two `suggest_links` drafts to one entity carry their own `name`, so one no longer
  replaces the other. `upsert_relationship` takes `prefer_this_link: true` to make the new link
  the route between its two entities where they met before through a shared lookup or through
  the rows of another fact; it moves no other route, and refuses, writing nothing, where it
  would have to. A refusal for moving that route says so, and a link whose pair a recorded route
  holds on a path that reads several rows is refused rather than written to change no answer.
  An `INCOMPATIBLE_TEMPORAL_ROLE` for a measure with no time role gives the line that dates it
  (`details.declare`), and validation and `promotion_check` warn `MEASURE_WITHOUT_TIME_ROLE` for
  such a measure whose rows reach a clock. See [suggest_links](docs/ARCHITECT_MCP.md).
