"""Load MetricFlow definitions from either a YAML directory or a
`semantic_manifest.json` artifact.

Both inputs normalize to the same in-memory shape:

    {
        "semantic_models": [ {..MetricFlow semantic_model body..}, ... ],
        "metrics":         [ {..MetricFlow metric body..}, ... ],
        "project":         { "time_spines": [...], ... } or {},
    }

The translator only consumes that shape — callers can plug in any
loader that produces it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml


def load_metricflow_dir(root: Path) -> dict[str, Any]:
    """Walk a directory of MetricFlow YAML files and bucket each
    top-level doc.

    Two authoring forms are recognized:

    A) MetricFlow standalone form (each doc has a single key):
       ``semantic_model:``, ``metric:``, ``project_configuration:``.
    B) dbt schema-2 form (multiple keys per doc):
       ``semantic_models: [ ... ]``, ``metrics: [ ... ]`` under
       ``version: 2``. dbt projects use this form. The ``model:``
       field on each semantic_model is a ``ref('...')`` string, not a
       ``node_relation`` block — we normalize it into the same shape
       the rest of the translator expects.
    """
    semantic_models: list[dict[str, Any]] = []
    metrics: list[dict[str, Any]] = []
    project: dict[str, Any] = {}

    for path in sorted(root.rglob("*.yaml")) + sorted(root.rglob("*.yml")):
        with path.open() as fh:
            for doc in yaml.safe_load_all(fh):
                if not isinstance(doc, dict):
                    continue
                # Form A: standalone single-key docs.
                if "semantic_model" in doc:
                    semantic_models.append(doc["semantic_model"])
                    continue
                if "metric" in doc:
                    metrics.append(doc["metric"])
                    continue
                if "project_configuration" in doc:
                    project = doc["project_configuration"]
                    continue
                # Form B: dbt schema-2.
                for sm in doc.get("semantic_models") or []:
                    semantic_models.append(_normalize_dbt_semantic_model(sm))
                for m in doc.get("metrics") or []:
                    metrics.append(m)

    return {
        "semantic_models": semantic_models,
        "metrics": metrics,
        "project": project,
    }


def _normalize_dbt_semantic_model(sm: dict[str, Any]) -> dict[str, Any]:
    """Convert a dbt schema-2 `semantic_models[*]` entry into the
    MetricFlow standalone shape. The only meaningful transform is
    rewriting ``model: "ref('foo')"`` into a synthetic
    ``node_relation.alias: foo``; everything else passes through
    unchanged."""
    out = dict(sm)
    model_ref = out.pop("model", None)
    if model_ref and "node_relation" not in out:
        alias = _strip_ref(model_ref) if isinstance(model_ref, str) else None
        if alias:
            out["node_relation"] = {"alias": alias}
    return out


def _strip_ref(text: str) -> str | None:
    """`ref('foo')` -> `foo`. Returns None for unrecognized shapes."""
    import re as _re

    text = text.strip()
    m = _re.match(r"ref\(\s*['\"]([\w.]+)['\"]\s*\)", text)
    if m:
        return m.group(1)
    # Some authors write `source('schema', 'table')` — flatten to the
    # table name so the downstream `relation:` is at least usable.
    m = _re.match(r"source\(\s*['\"][\w.]+['\"]\s*,\s*['\"]([\w.]+)['\"]\s*\)", text)
    if m:
        return m.group(1)
    return None


def load_semantic_manifest(path: Path) -> dict[str, Any]:
    """Load a dbt-emitted `semantic_manifest.json` and reduce it to the
    same shape produced by `load_metricflow_dir`.

    The manifest already nests `semantic_models`, `metrics`, and
    `project_configuration` at the top level, but each entry uses field
    names that differ slightly from the authoring YAML (e.g. snake_case
    is consistent, but `node_relation` is already an object). The shape
    is close enough that the translator can consume both.
    """
    with path.open() as fh:
        manifest = json.load(fh)

    return {
        "semantic_models": list(manifest.get("semantic_models", [])),
        "metrics": list(manifest.get("metrics", [])),
        "project": manifest.get("project_configuration", {}) or {},
    }


def load(source: Path) -> dict[str, Any]:
    """Auto-detect: a directory becomes `load_metricflow_dir`; a file
    ending in `.json` becomes `load_semantic_manifest`."""
    source = Path(source)
    if source.is_dir():
        return load_metricflow_dir(source)
    if source.is_file() and source.suffix == ".json":
        return load_semantic_manifest(source)
    raise ValueError(
        f"mf2sr: cannot infer input kind from {source!r}. "
        "Pass a MetricFlow YAML directory or a semantic_manifest.json file."
    )
