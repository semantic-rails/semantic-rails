- The Architect's `suggest_model` (and dbt import suggestions) flag a numeric column named like a
  count of distinct people (`unique`, `uniques`, `distinct`, `visitors`, `users`, `cloners`, but
  not an average or rate of one) as a low-confidence measure whose suggestion carries
  `additive: false` and asks the author to declare it, instead of calling `sum` "the usual
  default": a vendor's pre-counted uniques can't be added up across days or pages. The applied
  draft still sums the measure and leaves additivity unchanged, so an import or re-import never
  switches an existing measure off; declare it yourself. The REPL's `author model` leaves these
  columns unticked by default.
