- `validate runtime` (and `project validate --mode runtime`) also runs each segment's
  preview query, so a membership value the warehouse can't compare with its column is
  reported, with a hint, before `segment preview` fails.
