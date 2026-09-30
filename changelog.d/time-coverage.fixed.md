- Filled time buckets now use data observed outside the query window and stay NULL outside
  loaded base coverage, with future placeholder dates excluded and authored freshness honored.
- Coverage and observation scans respect policy row filters. Returned load-edge buckets carry
  a PARTIAL_BUCKET warning; observed measures outside coverage avoid a misleading scope warning.
