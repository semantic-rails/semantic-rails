- A sum filtered to August 24–30, 2026 now reads NULL when its only source row is dated
  September 7. It previously read 0, while the weekly window read NULL. Sums, counts and
  distinct counts also read NULL with a warning after the last loaded row. A quiet week
  between loaded rows still reads 0. A declared loaded-through date can keep that zero
  and adds a note when it extends past the last row. Empty weeks beyond the declared date
  read NULL. A sum of matching NULL amounts stays NULL and can report no data in scope, while
  a source with no loaded rows reports that coverage is unavailable. A metric that
  subtracts a constant from an uncovered sum now also reads NULL. A result filter asking
  for zero returns no row for an uncovered week and reports the empty result.
