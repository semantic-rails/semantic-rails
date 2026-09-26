- The Architect's `upsert_model` merges into each existing dimension, time, measure and join field
  by field. A label-only update to a time role used to replace the whole role with `{label: …}`,
  dropping its column, kind, class, default flag and grains while the parse gate still passed. A
  `null` field now removes that field, a `null` object is refused (use `remove_object`), and the
  report's `kept_fields` names the fields an update left as they were.
