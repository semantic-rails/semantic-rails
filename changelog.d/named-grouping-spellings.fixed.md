- Hold plans that drop a requested grouping expressed with a comma or whitespace
  after `by`, with `per` or `for each`, or with `level` or `grain`.
- Check every item in grouping lists before `level` or `grain`, including when filters
  consume the same dimensions. Complete plans keep readiness when a grouping is described
  twice or `level`, `grain`, or `per` is part of a declared object name.
