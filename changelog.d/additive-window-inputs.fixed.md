- Running and rolling ratios now divide the windowed numerator by the windowed
  denominator. Summing windows refuse statistics, stocks, distributions, and other
  inputs that do not add up across periods, including distinct counts of non-key columns
  or individual components of composite keys. An entity key cannot establish uniqueness
  when the measure's rows have a finer grain or come from another relation.
  Numeric literal multipliers and divisors preserve the windowed sum.
  Period-to-date on a non-default calendar refuses before execution instead of silently
  resetting on Gregorian periods.
