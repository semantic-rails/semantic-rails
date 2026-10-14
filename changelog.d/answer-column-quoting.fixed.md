- An `as` name that is a SQL keyword, such as `end`, `user` or `order`, now works on every
  warehouse: the SQL quotes the answer's column names where it defines and reads them. A table
  column that shares a name with an answer column is still read as the warehouse names it, also
  when the table shares its name with a relation the SQL names itself (`base`, `projected`).
