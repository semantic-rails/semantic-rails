- The [known limitations](docs/QUERY_CALL.md#known-limitations) now say where an answer trusts
  the data as stored: a child row whose parent id is missing counts in entity filters, a join
  on columns of different types or collations can double totals, and a parent grouped by a value
  only its child rows carry counts under each value. [Package authoring](docs/PACKAGE_AUTHORING.md)
  corrects the `prior_period` field (`offset: {unit, value}`) and graph entity keys (always a
  list), and documents `defaults.measure.meta`, package paths (relative, inside the package),
  dimension ids (named by entity, not model) and undecided routes (a valid end state).
