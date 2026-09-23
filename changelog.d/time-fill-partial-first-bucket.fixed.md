- A query with `time.fill: true` no longer drops a bucket at the edge of its
  window. The filled series was built from the calendar's bucket-start column
  filtered to the window, so a week or month that started before `start` was
  left out along with the rows it held from inside the window, and a bound with
  a time of day could drop its day. On `jaffle_shop`, orders for July 2017 by
  week lost the week of June 26, which holds July 1–2 (7,268 instead of 7,438
  orders), and a monthly query from July 15 lost the rest of July. The filled
  series now has every bucket that holds a day of the window, like the unfilled
  one, when the calendar has a date or timestamp `date_day` column.
