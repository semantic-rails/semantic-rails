- A select read per period (`cumulative`, `prior_period`, `rolling`, `period_to_date`, a
  conversion, a distribution) beside a time window without a grain is refused with
  `WINDOW_TOTAL_UNSUPPORTED` at `time.grain`, with the call's time block and a grain to add,
  instead of answering one row per raw timestamp. A call with only `group_by` over such a window
  lists each group once. `UNGRAINED_TIME_PROJECTION` and `UNGRAINED_GROUPED_TIME_PROJECTION`
  are gone.
- A balance read after its last snapshot says `NO_DATA_YET` and `window.data_through`, as a
  flow measure does.
- Refusal hints speak the call: the identity refusal names where each transport reads identity
  (MCP: the server's configured credentials), a malformed expression names its kind and path,
  a retired child group is shown as the entity filter that says the same thing, an unknown grain
  lists the grains, `EMPTY_GROUPS_UNSETTLED` names the call's outputs, and a `last` window
  reaching before 0001-01-01 is refused at `time.last.value` with the largest value the clock
  supports. A window at the calendar's last day answers instead of failing.
