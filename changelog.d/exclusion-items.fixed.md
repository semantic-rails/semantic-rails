- `plan` answers a question that excludes values ("excluding", "except", "without", "not",
  "but not", "other than", "apart from", "aside from", "minus", "outside of", "all stores
  but") with a `not` filter, which keeps the rows with no recorded value: "signups excluding
  web" counts the signups with no channel, and "revenue excluding Brooklyn" is ready to run.
  Before, such a draft dropped the rows with no recorded value, and a time exclusion
  ("signups not in June 2024") could be read as the question's window.
- An exclusion the draft doesn't leave out exactly with `not` filters still holds:
  `negation_reversed` when an `is` filter keeps an excluded value, otherwise
  `negation_unrealized`. The hold suggests asking for the breakdown by the excluded dimension
  instead (`ask_for_breakdown`), which shows each value and the rows with no value.
