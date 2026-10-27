- Semantic Rails runs on DuckDB 2.0 as well as 1.5.6 or later; a fresh install now resolves
  DuckDB 2.0. With 2.0, SQL run with bound parameters and a row cap no longer fails when it ends
  with `;` and a comment, `DESCRIBE`, `SHOW` and `SUMMARIZE` run as written, and duplicate column
  names keep their names. A DuckDB file written by DuckDB 2.0 can't be opened by 1.x.
