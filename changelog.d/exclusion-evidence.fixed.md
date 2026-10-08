- Hold plans unless an exclusion has a negative predicate on every value it names,
  instead of marking them ready to execute. Dropping only some of the values, an
  unrelated negative filter, a listed value the catalog can't match, or a temporal
  exclusion beside unrelated negative filters now holds with `negation_unrealized`.
