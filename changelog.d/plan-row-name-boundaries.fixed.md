- `plan` keeps words inside a resolved row name out of value filters, while preserving filter
  clauses outside that name. A selected metric beside an unquoted name ("Acme MRR") no longer
  becomes part of the name lookup; other name words remain intact.
