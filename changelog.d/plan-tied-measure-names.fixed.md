- When several measures or metrics match a question equally well, `plan` now picks the one
  the question names: "What is revenue by month?" uses a measure labelled Revenue, not Item
  Revenue Cents, which used to win on alphabetical order. When the question names none of
  them (Gross Revenue and Net Revenue for "revenue"), `plan` returns `low_confidence` with a
  `subject_ambiguous` gap that lists the candidates, and `ask` says which to name.
