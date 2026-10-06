- Refuse full-history cumulative, rolling, prior-period and period-to-date windows
  when a measure-bound temporal filter or matching clock row policy would remove
  lookback rows. Keep row-policy restrictions intact and omit inapplicable
  time-boundary recovery patches for authored and policy filters.
