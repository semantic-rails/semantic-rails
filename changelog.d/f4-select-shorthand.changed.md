- A select item sent as `{"metric": "<id>"}` or `{"measure": "<id>", "aggregation": "sum"}`
  without its `expression` wrapper, and a `{"dimension": "<id>"}` in `select[].expression`
  beside an empty `group_by`, are now accepted instead of refused with `Expression requires a
  'kind'`. They compile exactly like the canonical form and the response carries a
  `QUERY_SHORTHAND_NORMALIZED` warning naming it. Ambiguous shapes stay refused, and the error
  now shows the canonical form to send. See [Query IR schema](docs/QUERY_IR_SCHEMA.md).
