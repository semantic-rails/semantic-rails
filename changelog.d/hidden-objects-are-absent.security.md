- An object hidden from a caller (by `hidden`, or by a `visible_only` policy the caller is
  not eligible for) is now absent for that caller on every tool and HTTP operation: naming
  it gets the response of an object the package doesn't have, and no response to any
  other request names it, its policies' text, or what is computed from it. `hidden` now
  hides everything computed from its objects, as `visible_only` does; both actions require
  non-empty `object_ids`; raw-column aggregates are refused while anything is hidden from
  the caller; and a route through a hidden object is no route for that caller. Deny,
  withheld values, metric constraints and row filters apply exactly as before, and
  `plan` keeps its holds over measures that metrics govern, naming nothing hidden.
  Caller route decisions check row filters across the full package graph. Validity windows,
  discontinuities and MNPI flags keep their semantic structure when their text is omitted.
  `default_metric_id` names a metric only when that metric exists in the caller's view; otherwise it is empty.
  See [What a caller sees of a hidden object](docs/PACKAGE_AUTHORING.md#what-a-caller-sees-of-a-hidden-object).
