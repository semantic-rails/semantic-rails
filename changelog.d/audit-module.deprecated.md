- The audit sink (`AuditSink`, `StderrAuditSink`, `audit_logging_enabled`,
  `emit_audit_event`, `get_audit_sink`, `set_audit_sink`) moved from
  `semantic_rails.request_context` to the new `semantic_rails.audit` module, and
  `semantic_rails.embedding` now also exports `audit_logging_enabled`. Importing these names,
  or the API-key helpers that moved to `semantic_rails.api_keys`
  (`MISSING_API_KEY_FILE_SENTINEL`, `api_key_auth_result`, `configured_api_keys`,
  `extract_bearer_or_api_key`), from `semantic_rails.request_context` is deprecated and stops
  working in 0.3.3. Import them from their new modules (hosts: the audit names from
  `semantic_rails.embedding`), and install a sink with `set_audit_sink` rather than patching
  module state.
