- Row filters now also apply to the coverage reads behind a balance measure's `data_through`
  date and its `NO_DATA_YET` warning, so both reflect only the rows the caller can see; a
  coverage read a row filter can't be applied to refuses the request. A policy refusal while
  reading a balance's coverage or closing day now refuses the answer instead of dropping
  that disclosure.
