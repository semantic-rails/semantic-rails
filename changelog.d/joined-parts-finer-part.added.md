- **One finer part, and parts with no clock, in a joined call.** One part may be grouped by more
  than the join keys: each mailing list's owners beside its member count, reserves per
  instructor key beside the instructor listing by key and name, each person's salary beside the
  department's headcount (two people named Alex stay two rows). The other parts' values repeat
  on each of its rows of a key, and a key it lacks is one row with its columns NULL. A part whose
  selects read no clock (seats per room) answers beside a timed call, matched on every key but
  the period, its values repeated on each period's row. `PARTS_JOINED` names the finer part
  (`details.finer`), the parts with no clock (`untimed`) and every column that `repeats`, which
  is never stated to add up. Two finer parts, a finer part with no clock beside a grain, and a
  timed call none of whose parts has a clock are refused. See
  [Calls side by side](docs/QUERY_CALL.md#calls-side-by-side-parts-and-join).
