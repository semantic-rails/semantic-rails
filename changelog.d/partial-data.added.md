- New warning `PARTIAL_DATA`: a window total, or a row of a day-or-coarser series, that spans a
  whole day after `window.data_through` and before `now`'s day says how many of its days the
  data covers ("Data runs through 2026-10-10, so this total covers 6 of the window's 7 days").
  A Monday report over a week whose Sunday is not loaded yet no longer looks complete. A
  nightly load whose only gap is today's unfinished day says nothing.
