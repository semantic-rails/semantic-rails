- A filtered query now reads `0`, not `NULL`, for a sum or count with no rows when its measure
  has data anywhere else. Whether a measure has data is judged across its own rows, after its
  authored conditions and your row filters, ignoring the query's `where` filters: if store 5
  sold no apples, "apples at store 5" reads `0`, as store 5 does in a breakdown by store, and
  no `NO_DATA_IN_SCOPE` warning comes with it. A string `=` or `IN` `where` value that matches
  no row now adds one `FILTER_VALUE_NOT_FOUND` warning naming the value and the closest one, so
  a misspelling isn't read as a confident `0`. A measure whose authored condition never
  matched still reads `NULL`. Send `observation_scope: "query"`, or set
  `defaults.observation_scope: query` in the package, to judge inside the query's filters as
  before. Under the new default, a filtered query beside a `distribution`, or over a measure
  whose condition reads a fan-out, a metric predicate or a `CASE` below its top level, is
  refused with `EMPTY_GROUPS_UNSETTLED` and asks for `observation_scope: "query"`.
  See [Query IR schema](docs/QUERY_IR_SCHEMA.md#empty-groups-null-or-0).
