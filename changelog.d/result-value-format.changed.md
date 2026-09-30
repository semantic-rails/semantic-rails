- Query results use one JSON value format across warehouses and transports, with
  decimal columns encoded as precise strings, float and integer columns as numbers,
  ISO dates and times retaining full seconds precision, query-zone offsets for aware
  timestamps, and explicit naive metadata. Package snapshots, CLI tables and MCP
  segment previews retain and interpret the result column types.
