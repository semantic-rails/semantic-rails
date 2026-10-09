- Explain NULL periods beyond a measure's last bucket with data using `NO_DATA_YET`.
  Empty window totals name the resolved window start when visible data ends before it;
  coverage respects caller row filters and leaves SQL, values, and rows unchanged.
  Series compare SQL bucket keys at full precision, including hourly timestamps;
  empty series with dated coverage retain existing empty-window warnings.
