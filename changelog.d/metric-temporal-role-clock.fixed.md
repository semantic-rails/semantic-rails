- Package validation now rejects a metric whose `temporal_role` is not the
  clock of any of its measures, such as a ratio of order measures declared with
  the sessions table's clock. Such a metric still answered: each measure fell
  back to its own clock, with a `REWRITE_APPLIED` warning, but the result was
  labeled with the declared role, so one clock's series was presented as
  another's. A metric that mixes clocks is still accepted when one of its
  measures has the declared clock, and a conversion metric is timed by its base
  operand, as at query time. A package with such a metric now fails
  `validate-config` and `check`.
