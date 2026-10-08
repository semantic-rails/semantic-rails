- Fiscal and other non-default calendars are refused in this release, with or without
  `time.fill`, on every query shape: a non-default `time.calendar_id`, or a `grain` on a
  time whose model is bound to a non-default calendar, returns `REWRITE_NOT_SUPPORTED`
  with `details.reason: calendar_not_supported_yet` before any SQL runs. Some of these
  queries used to return Gregorian buckets under fiscal labels. Authored fiscal
  calendars return in a later release.
- Every fill, `rolling` and `prior_period` series now uses the implicit Gregorian
  calendar; an authored default calendar no longer supplies the series. ClickHouse,
  which has no implicit calendar, refuses them, and so does a query whose parts compile
  as separate sub-queries (a `distribution` beside a `rolling` or `prior_period`
  window), even when the package authors a default calendar.
