- A graph relationship's `as:` was ignored, and the relationship loaded under the id derived
  from its key. It now loads under its `as:` id, as every other object does.
- A package's semantic fingerprint no longer depends on the Python hash seed: a rollup
  variant's excluded entities and dimensions are listed in sorted order.
