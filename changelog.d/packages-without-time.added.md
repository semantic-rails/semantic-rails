- Accept packages that declare no time for counts, sums, ratios, grouping, filters
  and lookups. Explicit Query IR time requests refuse with an actionable `INVALID_TEMPORAL_ROLE`;
  project scaffolds accept a blank time column.
- Return every natural-language draft on a package without time as `low_confidence`,
  retaining its Query IR with a warning to check for a time breakdown or window.
