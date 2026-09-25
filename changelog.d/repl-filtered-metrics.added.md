- The REPL's `author metric` offers a filtered aggregate: one measure over only some rows,
  such as revenue from completed orders. Pick a dimension of the measure's model, "is one
  of" or "is not one of", and the values, from the dimension's declared domain or typed in.
  It is written as the aggregate expression with a filter, the same form the sample package
  uses.
- Reopening a supported filtered metric offers its saved dimension, operator, and values.
  Changing the measure or dimension requires selecting the new filter values. Expressions
  beyond this wizard's single-clause filter remain intact until a different recipe is chosen.
