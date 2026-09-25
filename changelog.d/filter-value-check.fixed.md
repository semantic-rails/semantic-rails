- A metric filter whose literal matches no value in the data, such as
  `status = 'Completed'` over a column that holds `completed`, passed
  validation silently, and the metric then returned an empty result with
  status `ok`. Validation that reads the data now warns with
  `FILTER_VALUE_NOT_FOUND` and names the closest value (`did you mean
  'completed'?`), and `project validate` shows runtime warnings in the
  `runtime` and `full` modes. Parse-only validation is unchanged.
