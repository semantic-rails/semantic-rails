- The REPL's `author metric` takes its defaults from the metric's own inputs. The time axis
  is the chosen measure's clock (a metric on orders is no longer reported on another table's
  date), and when the inputs offer more than one clock it asks. A ratio's denominator
  defaults to a count on the numerator's model, and its result type follows the inputs:
  revenue per order is currency per unit, other ratios default to a dimensionless ratio, and
  percent requires an explicit choice.
- Managing a metric reads it back as the package loads it. Inputs named by key, by
  namespace-qualified or custom name, explicit aggregations, filters, windows, currency and
  the time axis (including one set with `time`) are offered as the saved choices, so pressing
  Enter throughout leaves the metric as it was. Changing an input or recipe proposes the new
  input's result type, currency, aggregation and time axis instead, and a new filter dimension
  or measure asks for new filter values. The saved measure stays selected even when another
  measure's key spells its ID or a search would push it out of the short list.
- Expressions the wizard cannot write back, such as authored time expressions, several filter
  clauses or other derived formulas, stay unchanged unless another recipe is chosen. A saved
  window or offset unit the wizard cannot offer is refused rather than replaced.
- Editing a metric keeps its authored `examples` instead of replacing them.
- Plain REPL choices keep canonical option values when accepting uppercase defaults or
  case-insensitive typed answers, including filtered-metric `IN` and `NOT IN` operators.
- A new model proposes a singular entity key without clipping words such as `status` or
  `address` to `statu` or `addre`.
