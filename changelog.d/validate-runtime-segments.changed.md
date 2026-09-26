- `validate runtime` (and `project validate --mode runtime`) also runs each segment's
  preview query, so a membership value the warehouse can't compare with its column is
  reported, with a hint, before `segment preview` fails. `segment validate` stays
  warehouse-free: for a membership value on a text, date or time dimension, which it
  can't check against the column, it adds a `SEGMENT_VALUES_UNCHECKED` warning that names
  those commands.
