- An `as` name that is a SQL keyword, such as `end`, `user` or `order`, now works on every
  warehouse: the SQL quotes the answer's column names where it defines and reads them. A
  table's column that shares a name with an answer column keeps the table's own spelling, also
  when the table is named like a name the SQL gives its own steps, such as `base`.
