- A `scoped_aggregate` ratio whose numerator leaves out one of the denominator's `predicates`
  now divides its operands' own numbers. It read the numerator through both operands'
  predicates, so it could answer 0 where each operand alone read 1, and it left out a group
  only the numerator had instead of keeping it with a `NULL` denominator. Such a ratio no
  longer refuses a count or sum threshold that zero satisfies either. A numerator that keeps
  every denominator predicate and adds its own still reads as a share of the whole.
