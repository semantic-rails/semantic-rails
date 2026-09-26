- The Architect's `suggest_model` and dbt import drafts treat a numeric column named like a count of
  distinct people (`unique`, `uniques`, `distinct`, `visitors`, `users`, `cloners`) as a
  low-confidence measure drafted `additive: false`, instead of proposing `sum` as "the usual
  default": a vendor's pre-counted uniques can't be added up across days or pages.
