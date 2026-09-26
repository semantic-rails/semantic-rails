- A rollup can declare `requires_certification: true`. It then routes only while
  the host's certification provider, installed with
  `semantic_rails.acceleration.routing.set_certification_provider`, says it is
  certified. With no provider it runs on the base tables (`not_certified`). A
  package with such a rollup skips the compile cache and compiles every request,
  so a revoked certification applies to the next one.
  `semantic_rails.acceleration.certification.certify_aggregate_relation(config,
  relation_id)` returns the engine's verdict on each of a rollup's measure columns
  with a paired base and rollup query to compare before certifying it. Rollup rows
  in validation metadata gain a `requires_certification` field.
