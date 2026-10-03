- Running and rolling ratios now divide the windowed numerator by the windowed
  denominator. Summing windows refuse statistics, stocks, distributions, and other
  inputs that do not add up across periods. Period-to-date on a non-default calendar
  refuses before execution instead of silently resetting on Gregorian periods.
