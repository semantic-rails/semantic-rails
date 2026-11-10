- A call whose `select` nests expressions deeply (a long arithmetic chain, nested `case`,
  `boolean` or `prior_period`, or an `aggregate_if` condition with many terms) now validates,
  compiles and runs in time proportional to its size. Seventeen 200-level arithmetic chains
  took 44 seconds to validate and now take 2; the compiled SQL is unchanged.
