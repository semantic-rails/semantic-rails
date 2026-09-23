- The REPL's `author metric` takes its defaults from the metric's own inputs. The time axis
  is the chosen measure's model clock (a metric on orders is no longer reported on another
  table's date), and when the inputs offer more than one clock it asks. A ratio's
  denominator defaults to a count on the numerator's model, and its result type follows the
  inputs: revenue per order is currency per unit, a share of like with like is a percent,
  anything else a ratio. The measure a metric needs can be created from inside the metric
  wizard; cancelling the metric takes that measure back, and one `undo` reverts both.
