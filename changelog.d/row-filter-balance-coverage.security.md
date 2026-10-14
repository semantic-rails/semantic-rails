- Row filters now also apply to the coverage reads behind a balance measure's `data_through`
  date and its `NO_DATA_YET` warning, so both reflect only the rows the caller can see; a
  coverage read a row filter can't be applied to refuses the request. A policy refusal while
  reading a balance's coverage or closing day now refuses the answer instead of dropping
  that disclosure.
- A row-filter refusal's `policy_ids` name only the policies the caller can see; a row
  filter hidden from the caller is listed as `row_filter`.
- A missing or mistyped request attribute of a row filter hidden from the caller is refused
  as `POLICY_DENIED` with reason `row_filter_denied`, naming no attribute; an attribute of a
  row filter the caller can see is still named.
