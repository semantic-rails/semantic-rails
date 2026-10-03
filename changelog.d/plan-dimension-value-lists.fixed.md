- Plan the question's values from one list phrase on one dimension as one filter
  (`=` for one value, `in` for several) for one total or combined ranking, without
  adding grouping.
  Keep caller filter rows as written, with surrounding field whitespace removed;
  append generated rows unless an identical row already exists.
  Keep separate equality clauses as separate predicates and report contradictions
  without execute readiness.
