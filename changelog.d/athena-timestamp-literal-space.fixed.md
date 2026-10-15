- Athena: a window bound with a time of day (a `this` window ending at `now`) is written as
  a Trino `TIMESTAMP '2024-08-03 12:00:00'` literal, with a space instead of ISO's `T`, which
  Trino does not read as a timestamp.
