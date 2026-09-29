- A select item sent as `{"metric": "<id>"}` or `{"measure": "<id>", "aggregation": "sum"}`
  without its `expression` wrapper, and a `{"dimension": "<id>"}` in `select[].expression`
  beside an empty `group_by`, are now accepted instead of refused with `Expression requires a
  'kind'`. They compile exactly like the canonical form, and `plan` accepts the same shapes in
  its `query`. Every rewrite, including the existing bare `{"dimension": "<id>"}` select item,
  now adds a `QUERY_SHORTHAND_NORMALIZED` warning naming the canonical form.
- A rewrite never drops a key: a select item naming more than one of `metric`, `measure` and
  `dimension`, a dimension item with `as` or any other key, and `expression` beside `metric`,
  `measure` or `dimension` are refused with the canonical form in the message. The bare
  `{"dimension": "<id>", "as": "<alias>"}` item, which used to lose its alias, is refused too.
  An ungrained-time warning now reads the query after the rewrite, so a shorthand dimension
  counts as grouped. See [Query IR schema](docs/QUERY_IR_SCHEMA.md).
