- A conversion operand whose measure counts anything other than its entity's
  rows is now rejected with `CONVERSION_NOT_SUPPORTED`: a measure that counts an
  expression, such as an `entity_count` measure with a `CASE WHEN ... THEN key
  END` filter, a column other than the entity's key (spelled as the entity
  declares it, case included), or a fact model's rows.
  Before, the conversion counted every row of the entity and silently dropped
  the measure's definition. On `jaffle_shop`, a new-customer-order-to-large-order
  rate with `large_order_count` as the converted operand came out as 1.0
  instead of 0.31: every order counted as a large order, so each base order
  converted to itself. A package with a curated conversion metric built on such
  a measure now fails `validate-config` and `check`, and `discover` lists the
  metric as unavailable. Instead of a filtered measure, count the entity key and
  restrict the operand with its `filter`. Instead of a measure counting another
  column, use a measure on the entity whose rows are the events.
