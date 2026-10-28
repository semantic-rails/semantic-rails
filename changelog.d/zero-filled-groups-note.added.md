- `execute` adds the note `ZERO_FILLED_GROUPS` when a count or sum reads `0` in groups its own
  rows never reach while another selected measure's rows hold them ("signups and orders by
  customer": a customer who never ordered reads 0 orders). It counts those groups among the
  returned rows ("at least" for a truncated answer), names the entity with no rows, and gives
  the `having` condition that leaves them out. The answer's own statement marks the groups, so
  no other query runs; a group that reads `NULL` (no data) or read rows that sum to 0 is never
  counted, and a series' empty periods are not groups. See
  [Empty groups](docs/QUERY_CALL.md#empty-groups-null-or-0).
