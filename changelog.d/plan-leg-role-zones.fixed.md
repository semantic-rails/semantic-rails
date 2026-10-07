- Planning holds a relative window that any leg of a metric reads on other days: when a leg is
  read on its own temporal role (because it can't read the selected one) and that role's zone
  reads the question's window as other days than the planning zone, the plan is held with
  `TIME_WINDOW_UNRESOLVED` and returns no query, instead of reading that leg a day early or
  late. `why.details.temporal_roles` names each such role and its zone. Absolute windows are
  unchanged.
