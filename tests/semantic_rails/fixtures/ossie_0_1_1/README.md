# Apache Ossie 0.1.1 validator (vendored)

Unmodified copies from [apache/ossie](https://github.com/apache/ossie), tag `osi-0.1.1-rc1`
(commit `faf581054dcf7964d5fe0ceae7d6f415c8ce32a5`), so the Ossie export tests run the spec's own
validator without a network fetch. `tests/semantic_rails/test_ossie_export.py` pins each file's
SHA-256; replace the files together and update the pins to move to another spec version.

| File | Upstream path |
| --- | --- |
| `core-spec/osi-schema.json` | `core-spec/osi-schema.json` |
| `validation/validate.py` | `validation/validate.py` |
| `NOTICE` | `NOTICE` |

Licensed under the Apache License, Version 2.0, the same terms as this repository's
[LICENSE](../../../../LICENSE). `ruff.toml` keeps the linter and formatter off the vendored
validator.
