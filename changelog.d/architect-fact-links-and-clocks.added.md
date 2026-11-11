- A package built with the Architect can answer by month and across two facts. `suggest_links`
  proposes a link on a key of two or more columns (a return and the sale line it names by
  `sale_order_id, sale_line_no`), saying the target key is never null and never repeated, the
  columns matched and how many rows find a match; a target whose columns repeat together is
  never drafted, and a tuple that is not the table's own key is drafted `many_to_one`.
  `suggest_model` with `project_path` dates the measures of a table that keeps its dates as
  calendar keys: one key, or of several the one named after the row (`sale_day_key` on sales),
  with the others as named roles; beside date columns of its own, the one named after the row of
  all of them. Only a calendar dates through a key, never another entity's own date; with
  several own date columns, the one named after the row is the default. Where no choice is
  defensible, the draft dates none and `time_roles` says the choice is yours. Two
  `suggest_links` drafts to one entity carry their own `name`, so one no longer replaces the
  other. `upsert_relationship` takes `prefer_this_link: true` to make the new link the route
  between its two entities where they met before through a shared lookup or through the rows of
  another fact, and a refusal for moving that route says so. An `INCOMPATIBLE_TEMPORAL_ROLE` for
  a measure with no time role gives the line that dates it (`details.declare`), and validation
  and `promotion_check` warn `MEASURE_WITHOUT_TIME_ROLE` for such a measure whose rows reach a
  clock. See [suggest_links](docs/ARCHITECT_MCP.md).
