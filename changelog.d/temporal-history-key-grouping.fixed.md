- Apply declared temporal-validity joins when grouping or filtering by a history
  key reached by a hop into the validity window, including NULL for missing versions;
  require a query time even when the source has a matching key column. Hops out of
  the table holding the window keep the source-key shortcut.
- Distinguish Jaffle Shop's historical customer key from its customer key when
  planning all-time customer rankings.
