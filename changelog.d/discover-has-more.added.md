- `discover` says when a list was cut: when a bucket had more matches than `limit`, `has_more`
  names it with how many more it has (`"has_more": {"dimensions": 5}`), at every verbosity, over
  MCP and REST. It counts only objects the caller may see, so a hidden or ungranted object never
  counts. To keep the default MCP response within its budget, the minimal and compact responses
  now leave out empty buckets and the ranking `stage`. See
  [Catalog And Metadata](docs/MCP_INTERFACE.md#catalog-and-metadata).
