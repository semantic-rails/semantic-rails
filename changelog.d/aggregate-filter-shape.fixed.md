- An aggregate's `filter` must now be `{all: [...]}`. Other shapes loaded and
  validated anyway. An `any:` list, or `any:` next to `all:`, was silently
  ignored, so the aggregate counted every row. A bare expression node such as
  `{kind: comparison, ...}` passed package validation, and queries then failed
  with a generic `Unsupported expression kind.` error. An `all:` holding one
  condition instead of a list, or a list in place of the mapping, crashed with
  an internal error. These shapes are now rejected with `INVALID_EXPRESSION_AST`
  and the expected shape, in queries and at package validation. A conversion
  operand's `any:` filter reports `INVALID_EXPRESSION_AST` instead of
  `CONVERSION_NOT_SUPPORTED`.
- Falsy malformed filter values (`[]`, empty text, zero, and false) and invalid
  entries under `all:` now fail closed instead of disappearing and producing
  unfiltered counts. The package loader preserves supplied filter values so
  the same validation applies to authored metrics. The package-authoring
  example now uses the supported `all:` predicate form.
