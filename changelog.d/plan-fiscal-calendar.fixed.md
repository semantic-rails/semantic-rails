- `plan` counts a fiscal question's time on the fiscal calendar. "Revenue by fiscal quarter"
  came back as Gregorian quarters with status ok, and "vs prior fiscal quarter" was dropped
  with status ok. With one calendar whose name says fiscal, `plan` puts a question that asks
  for fiscal buckets ("by fiscal quarter") on it (`time.calendar_id` with `time.fill: true`).
  Any other fiscal period ("the first fiscal quarter"), or a package without such a calendar,
  returns `low_confidence` with a `fiscal_calendar_unrealized` gap, and a dropped fiscal
  comparison is reported like any other. A fiscal question's window resolves only from exact
  days: "fiscal Q2 2017", "FY2017" and "last fiscal quarter" return `TIME_WINDOW_UNRESOLVED`
  instead of the Gregorian period of the same name.
- `plan` with a partial query it can't read (`group_by: [["dimension.x"]]`) returns
  `INVALID_QUERY` with a recovery hint instead of an internal error, and a select item passed
  in `query` appears once, under the caller's alias, instead of again under the draft's.
