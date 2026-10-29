- A filtered metric over a table that declares how far it is loaded (`zero_through`) reads `0`
  in every loaded period even when its filter has never matched a row, such as a count of
  enterprise accounts before the first one signs up; it read `NULL` with `NO_DATA_IN_SCOPE`.
  Periods after the edge still read `NULL` with `NO_DATA_YET`, so no `COALESCE(..., 0)` is
  needed. See [Empty periods](docs/PACKAGE_AUTHORING.md#empty-periods).
