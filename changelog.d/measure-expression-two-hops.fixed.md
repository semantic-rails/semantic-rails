- A `sum` or `count` measure whose expression reads a column of an entity two or more hops
  away now aggregates after its joins, instead of pre-aggregating its own table first and
  failing in the warehouse on the column it could not see.
