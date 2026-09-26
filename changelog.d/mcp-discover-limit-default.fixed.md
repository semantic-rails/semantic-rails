- The query MCP `discover` schema no longer advertises a `limit` default, so a client that
  fills in schema defaults gets 100-id pages for empty `terms`, not 10. Two recovery hints that
  pointed MCP agents at HTTP routes now name `discover`.
