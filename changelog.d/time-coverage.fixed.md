- Empty time buckets use observation outside bounded query windows and remain NULL
  outside loaded base coverage. Coverage gates only zero filling and preserves populated
  values, including NULL time keys and future dates. Its current-time cap honors role zones.
- Coverage and observation scans respect policy row filters. Unbounded plain and routed
  queries avoid extra scans, and execution adds no coverage diagnostic statement.
