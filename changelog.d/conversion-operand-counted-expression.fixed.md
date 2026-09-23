- A conversion operand whose measure counts an expression, such as an
  `entity_count` measure with a `CASE WHEN ... THEN key END` filter, or a
  column other than its entity's key, is now rejected with
  `CONVERSION_NOT_SUPPORTED`. Before, the conversion counted every row of the
  entity and silently dropped the filter: a first-order-to-repeat-order rate
  could come out as 1.0. Restrict an operand with its `filter` instead.
