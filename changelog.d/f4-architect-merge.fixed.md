- The Architect's `upsert_model` no longer wipes an existing dimension, time, measure or join when
  you only relabel it. A label-only update to a time role used to replace the whole role with
  `{label: …}`, dropping its column, kind, class, default flag and grains while the parse gate
  still passed. An update that names only `label`, `description`, `synonyms` or `meta` now keeps
  the object's other fields. Any other update still rewrites the object, and the report's
  `dropped_fields` now names each field that rewrite drops.
