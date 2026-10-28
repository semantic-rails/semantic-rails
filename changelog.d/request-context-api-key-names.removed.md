- `semantic_rails.request_context` no longer re-exports the API-key helpers
  (`MISSING_API_KEY_FILE_SENTINEL`, `api_key_auth_result`, `configured_api_keys`,
  `extract_bearer_or_api_key`); import them from `semantic_rails.api_keys`.
  `warn_if_default_policy_resolver_exposed` moved to `semantic_rails.http_core`. Neither module
  is part of the embedding facade.
