- Breaking change within 0.x: Query IR `version: 1` is the only supported version;
  `version: 2` is refused with `INVALID_QUERY` and `details.supported_versions: [1]`.
  Planner outputs use version 1, and the preview-v2 contract is no longer shipped.
  Move a version 2 query by changing only its version number to 1; the schemas
  had identical query shapes.
