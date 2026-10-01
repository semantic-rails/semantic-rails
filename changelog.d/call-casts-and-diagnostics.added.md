- Add numeric and text CAST expressions with dialect-specific SQL types and
  NULL preservation. Scalar calls now advertise the accepted warehouse functions,
  report known argument mismatches as `CALL_ARGUMENT_TYPE`, and explain how to
  fix literal-only selects before execution.
