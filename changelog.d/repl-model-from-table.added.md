- In a DuckDB package whose database can be read, the REPL's `author model` starts from the
  warehouse: pick a table (already modeled tables are marked), then confirm the suggested
  entity, key, time columns, dimensions and measures as prefilled checkboxes, and which
  measures are money amounts. The whole model is written in one change with a preview, a
  parse check and `undo`. Detected links to other tables are listed. Without a readable
  database, `author model` asks for the table by name as before.
