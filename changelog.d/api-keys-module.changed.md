- The API-key helpers (`api_key_auth_result`, `configured_api_keys`,
  `extract_bearer_or_api_key`) moved from `semantic_rails.request_context` to the new
  `semantic_rails.api_keys` module. Importing them from `semantic_rails.request_context`
  still works.
