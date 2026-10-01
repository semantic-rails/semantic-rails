- Restrict external access and extension loading on read-only DuckDB
  execution, bootstrap probe and authoring introspection connections.
- Refuse nonempty authored Snowflake tags on named-profile connections before
  connecting, removing manual tag SQL while preserving profile session settings.
