- A group whose rows all have a NULL amount reads `NULL` in two more shapes, as it already did
  elsewhere ("All NULL reads NULL"), in single periods and windows alike. Beside a
  `distribution`, "revenue and median order by store by month" read `0` for a store's month whose
  only order has no amount, and "trailing three-month revenue beside the median order" did so
  for the windows that hold only that order. A sum measure with a `CASE` that has an `ELSE`
  below its root, such as a tax multiplier (`amount * CASE WHEN store_id = 'a' THEN 1.1 ELSE 1.2
  END`), read `0` there too. A group with no rows still reads `0`.
- Beside a distribution, each output answers as it does alone: a conditional `CASE` measure
  there reads its base table, not a rollup, as it does without the distribution.
