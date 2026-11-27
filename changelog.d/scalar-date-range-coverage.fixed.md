- A scalar sum filtered to August 24–30, 2026 now reads NULL when its only source row is
  dated September 7. It previously read 0, while the weekly window read NULL. A quiet week
  between loaded rows or covered by a declared loaded-through date still reads 0.
