- `plan` answers a question that asks several things one draft can't answer ("Last week, how
  many accounts signed up, and what was the MRR?") as `parts`: one plan per clause, each with
  its own `status`, `best` and `why`, ready only when every part is. A part that points back at
  another, names nothing, or lacks a window or filter another part states is held with
  `PLAN_PARTS_HELD`, as are parts that don't all state the same grouping; the held draft's
  gaps stay in `why.details.gaps`. The MCP `plan` output schema declares `parts`, and the server
  instructions say to execute each part's query. `ask`, which runs one query, refuses such a
  question with `PLAN_PARTS` and lists its parts.
