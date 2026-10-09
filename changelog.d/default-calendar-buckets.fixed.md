- A package's authored default calendar no longer changes time buckets: its `date_day`
  and `week_start` to `year_start` columns are not read for bucketing or filling, so
  filled weeks are ISO Monday weeks even when its `week_start` names Sundays (these
  read as silent NULLs before), and a calendar whose days are stored at noon no longer
  drops the bucket of an intraday window. Its columns remain ordinary dimensions.
