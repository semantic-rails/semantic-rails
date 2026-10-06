- Refuse full-history cumulative, rolling, prior-period and period-to-date windows
  when a measure-bound temporal filter or matching clock row policy would remove
  lookback rows. Keep row-policy restrictions intact and omit inapplicable
  time-boundary recovery patches for authored and policy filters.
- Return only the code and message for a policy denial when a denied query reads
  an object hidden from the caller or with undetermined visibility, omitting
  `blocked_objects`, `policy_effects`, `policy_violations` and hints.
