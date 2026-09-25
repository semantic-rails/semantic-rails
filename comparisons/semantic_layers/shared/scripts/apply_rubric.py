"""Assign every support label from each layer's committed artifacts with one rule (shared/rubric.md).

Nobody labels a layer by hand, Semantic Rails included. Each label records the rule that fired
and its evidence, so a reader can check it against the artifacts.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from semantic_rails.config import _merge_package_dir

REPO_ROOT = Path(__file__).resolve().parents[4]
PACK = REPO_ROOT / "comparisons" / "semantic_layers"
RESULTS = PACK / "shared" / "results"
OUTPUT_PATH = RESULTS / "rubric" / "labels.json"

# A trivial passthrough of one shared view isn't hand-written logic.
PASSTHROUGH = re.compile(r"^\s*select\s+\*\s+from\s+comparison_\w+\s*;?\s*$", re.IGNORECASE)
RULES = [
    ("unsupported", "The layer didn't execute the question."),
    (
        "precomputed",
        "The answer reads a cross-row rollup column that the question declares in "
        "bypass_columns, in the executed SQL or in SQL it depends on.",
    ),
    (
        "workaround",
        "The answer depends on SQL written by hand for this pack: a derived table, a "
        "SQL-defined source or cube, or a statement outside the layer's semantic interface.",
    ),
    ("native", "None of the above: the layer's semantic constructs only."),
]


def is_passthrough(sql: str) -> bool:
    body = re.sub(r"\{\{.*?\}\}", "", sql, flags=re.DOTALL)
    body = "\n".join(line for line in body.splitlines() if not line.strip().startswith("--"))
    return PASSTHROUGH.match(body.strip()) is not None


def reads_column(sql: str, column: str) -> bool:
    return re.search(rf"\b{re.escape(column)}\b", sql, flags=re.IGNORECASE) is not None


def decide(
    executed: bool, helpers: list[str], sql_texts: list[str], bypass: list[str]
) -> dict[str, Any]:
    """Apply the rules in order; the first that holds labels the answer."""
    if not executed:
        return {"label": "unsupported", "evidence": []}
    bypassed = [column for column in bypass if any(reads_column(t, column) for t in sql_texts)]
    if bypassed:
        return {"label": "precomputed", "evidence": [f"reads {column}" for column in bypassed]}
    if helpers:
        return {"label": "workaround", "evidence": sorted(helpers)}
    return {"label": "native", "evidence": []}


def _read(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def _executed_sql(entry: dict[str, Any]) -> str:
    """Fail closed: an executed answer whose SQL is missing can't be checked for bypass columns."""
    path = entry.get("sql_path")
    text = _read(path) if path and (REPO_ROOT / path).is_file() else ""
    if not text.strip():
        raise SystemExit(f"{entry['question_id']}: found no executed SQL to check")
    return text


def _require(found: list[str], what: str, entry: dict[str, Any]) -> list[str]:
    """Fail closed: a detector that finds nothing to check can't vouch for an answer."""
    if not found:
        raise SystemExit(f"{entry['question_id']}: found no {what} to check")
    return found


SR_PACKAGE = PACK / "semantic_rails" / "package"
SHARED_VIEW = re.compile(r"comparison_\w+")


def semantic_rails(entry: dict[str, Any]) -> tuple[list[str], list[str]]:
    # The rubric doesn't resolve which models a metric reads, so any derived relation in the
    # package counts against every answer: a relation pipeline, an aggregate relation, or a
    # model over anything but one shared view. The package is read the way the engine reads
    # it, so a model declared anywhere the engine accepts one is checked.
    package = _merge_package_dir(str(SR_PACKAGE))
    helpers = []
    if package.get("relations") or package.get("aggregate_relations"):
        helpers.append("relation pipelines")
    models = _require(sorted(dict(package.get("models") or {}).items()), "models", entry)
    for model_id, model in models:
        relation = model.get("relation")
        if (
            {"relation_ref", "variants"} & set(model)
            or not isinstance(relation, str)
            or not SHARED_VIEW.fullmatch(relation)
        ):
            helpers.append(f"derived model {model_id}")
    return helpers, [_executed_sql(entry)]


MF_MODELS = PACK / "metricflow" / "models"


def metricflow(entry: dict[str, Any]) -> tuple[list[str], list[str]]:
    executed = _executed_sql(entry).split("SQL (remove --explain", 1)[-1]
    config = yaml.safe_load((MF_MODELS / "_models.yml").read_text(encoding="utf-8"))
    # MetricFlow requires a time spine model: it's configuration, not logic for this pack.
    time_spines = {model["name"] for model in config.get("models", []) if "time_spine" in model}
    models = {path.stem: path for path in MF_MODELS.rglob("*.sql")}
    helpers, texts = [], [executed]
    relations = sorted(set(re.findall(r'"main"\."(\w+)"', executed)))
    for relation in _require(relations, "relations in the executed SQL", entry):
        if relation not in models or relation in time_spines:
            continue
        body = models[relation].read_text(encoding="utf-8")
        if not is_passthrough(body):
            helpers.append(f"dbt model {relation}")
            texts.append(body)
    return helpers, texts


def cube(entry: dict[str, Any]) -> tuple[list[str], list[str]]:
    query = json.loads(_read(entry["query_path"]))
    members = [*query.get("measures", []), *query.get("dimensions", [])]
    members += [item["dimension"] for item in query.get("timeDimensions", [])]
    members += [item["member"] for item in query.get("filters", []) if "member" in item]
    helpers = []
    for name in _require(sorted({member.split(".")[0] for member in members}), "cubes", entry):
        model = PACK / "cube" / "model" / "cubes" / f"{name}.yml"
        spec = yaml.safe_load(model.read_text(encoding="utf-8"))["cubes"][0]
        if "sql" in spec and not is_passthrough(spec["sql"]):
            helpers.append(f"cube {name}")
    executed = json.loads(_executed_sql(entry))["sql"]["sql"][0]
    return helpers, [executed]


# `name is jaffle.sql("""...""")`: a SQL source, or a join declared inline.
MALLOY_SQL = re.compile(r"(\w+)\s+is\s+\w+\.sql\(\s*(\"\"\"|\"|')(.*?)\2\s*\)", re.DOTALL)


def _squash(sql: str) -> str:
    """Lowercase, single spaces, and no space just inside parentheses."""
    return re.sub(r"\(\s+|\s+\)", lambda m: m.group().strip(), " ".join(sql.split()).lower())


def malloy(entry: dict[str, Any]) -> tuple[list[str], list[str]]:
    model = (PACK / "malloy" / "models" / "jaffle.malloy").read_text(encoding="utf-8")
    query = rf"^query:\s+{re.escape(entry['question_id'])}\s+is\b"
    _require(re.findall(query, model, flags=re.MULTILINE), "named query", entry)
    executed = _executed_sql(entry)
    # Malloy compiles a SQL block into the query verbatim, as a parenthesized derived table,
    # wherever the query reads it: as its source, through a join or alias, or under an `extend`.
    # Matching the whole parenthesized body keeps a block that is only part of another's SQL
    # from counting.
    helpers = {
        f"SQL source {name}"
        for name, _, body in MALLOY_SQL.findall(model)
        if not is_passthrough(body) and f"({_squash(body)})" in _squash(executed)
    }
    return sorted(helpers), [executed]


def ktx(entry: dict[str, Any]) -> tuple[list[str], list[str]]:
    payload = json.loads(_read(entry["query_path"]))
    fields = [*payload.get("measures", [])]
    fields += [
        item if isinstance(item, str) else item["field"] for item in payload.get("dimensions", [])
    ]
    fields += [re.split(r"\s", item)[0] for item in payload.get("filters", [])]
    helpers = []
    for name in _require(sorted({field.split(".")[0] for field in fields}), "sources", entry):
        spec = yaml.safe_load(
            (PACK / "ktx" / "sources" / f"{name}.yaml").read_text(encoding="utf-8")
        )
        if "sql" in spec and not is_passthrough(spec["sql"]):
            helpers.append(f"SQL source {name}")
    return helpers, [_executed_sql(entry)]


def snowflake(entry: dict[str, Any]) -> tuple[list[str], list[str]]:
    executed = _executed_sql(entry)  # the statement the capture ran
    helpers = [] if "SEMANTIC_VIEW(" in executed else ["SQL outside SEMANTIC_VIEW(...)"]
    return helpers, [executed]


DETECTORS = {
    "semantic_rails": ("semantic_rails", semantic_rails),
    "metricflow": ("metricflow", metricflow),
    "cube": ("cube", cube),
    "malloy": ("malloy", malloy),
    "snowflake_semantic_views": ("snowflake_semantic_views", snowflake),
    "ktx": ("ktx", ktx),
}


def build_labels() -> dict[str, Any]:
    questions = yaml.safe_load((PACK / "shared" / "questions.yml").read_text(encoding="utf-8"))
    labels: dict[str, dict[str, Any]] = {}
    for layer, (results_dir, detect) in DETECTORS.items():
        directory = RESULTS / results_dir
        summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
        entries = {entry["question_id"]: entry for entry in summary["questions"]}
        failed_path = directory / "unsupported.json"
        failed = json.loads(failed_path.read_text(encoding="utf-8")) if failed_path.exists() else {}
        labels[layer] = {}
        for question in questions["questions"]:
            # A question the layer has no executed result for is unsupported, whatever the reason.
            entry = entries.get(question["id"])
            executed = (
                entry is not None
                and entry["status"] != "unsupported"
                and question["id"] not in failed
            )
            helpers, texts = detect(entry) if executed else ([], [])
            labels[layer][question["id"]] = decide(
                executed, helpers, texts, list(question.get("bypass_columns", []))
            )
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "rules": [{"label": label, "rule": rule} for label, rule in RULES],
        "labels": labels,
    }


def main() -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(build_labels(), indent=2, sort_keys=True), encoding="utf-8")
    print(f"Wrote rubric labels to {OUTPUT_PATH.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
