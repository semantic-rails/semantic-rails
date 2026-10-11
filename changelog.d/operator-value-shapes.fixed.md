- `query.v2.json` states each filter operator's value shape (one value for the ranges, text for
  `contains`, true or false for `missing`, one value or a list for `is` and `not`) and reads
  operator words and aliases in any case and spacing, as the parser does.
