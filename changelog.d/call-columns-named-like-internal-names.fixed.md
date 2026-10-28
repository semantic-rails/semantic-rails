- A call's column named `t`, `g1`, `m1`, `p1` (or another name the engine gives the period, a
  group key, a measure or a result filter inside the statement, in any case) reads its own
  values. Beside a `having` it used to read the period or a group key (a store's name in place of
  its order count), or be refused beside a `limit` with "Limited ordering must resolve to an
  output column". The engine now gives its own columns names the call doesn't use.
