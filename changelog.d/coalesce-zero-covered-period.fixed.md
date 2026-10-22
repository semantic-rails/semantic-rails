- A metric wrapped in `COALESCE(…, 0)` reads 0 for a period that has data but none that matches
  the query's `where`, and keeps that period's row; before 0.3.2 the row was dropped (or the
  answer came back empty for a one-period window). The canonical answers now hold this on DuckDB
  and Postgres.
