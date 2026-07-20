"""Deterministic MetricFlow -> Semantic Rails package translator.

Reads either a MetricFlow YAML directory (multi-doc `semantic_model:` /
`metric:` files) or a parsed `semantic_manifest.json`, and emits a
complete Semantic Rails package directory that loads under
`semantic-rails parse-config --package <id>`.

No LLM. Pure schema mapping with conservative fallbacks for shapes that
do not round-trip cleanly (string filter expressions, complex derived
formulas).
"""

from .translate import TranslationReport, translate

__all__ = ["translate", "TranslationReport"]
