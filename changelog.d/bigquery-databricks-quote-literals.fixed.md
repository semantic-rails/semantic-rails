- BigQuery and Databricks: a quote inside a filter value is written `\'`. BigQuery refused
  the doubled quote (`''`) as two string literals, and Databricks read it as two literals
  joined together, which dropped the quote from the value.
