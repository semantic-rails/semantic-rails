- A relationship's `temporal_validity` must now declare both `valid_from` and `valid_to`.
  A package whose window has only one of them, or blank columns, fails to load with
  `INVALID_CONFIG`. Previously, a row could join every version that had started or had
  not ended and be counted once per version. Add the missing column. A NULL in the
  `valid_to` column still means the version is current.
